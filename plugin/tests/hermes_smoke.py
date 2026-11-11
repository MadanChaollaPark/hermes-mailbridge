"""Run against an actual Hermes checkout: python tests/hermes_smoke.py --hermes-source /path/to/hermes-agent."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', required=True, type=Path)
    args = parser.parse_args()
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(args.hermes_source.resolve()))
    with tempfile.TemporaryDirectory(prefix='mailbridge-host-test-') as tmp:
        home = Path(tmp)
        bundled = home / 'empty-bundled'
        bundled.mkdir()
        os.environ.update(HERMES_HOME=str(home), HERMES_BUNDLED_PLUGINS=str(bundled),
                          HERMES_ENABLE_PROJECT_PLUGINS='0', HERMES_SAFE_MODE='0')
        shutil.copytree(package, home / 'plugins/mailbridge',
                        ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv', 'runtime'))
        (home / 'config.yaml').write_text('plugins:\n  enabled:\n    - mailbridge\n')
        from hermes_cli.plugin_dev import doctor_plugin
        from hermes_cli.plugins import PluginManager
        from tools.registry import registry
        doctor = doctor_plugin(package)
        assert doctor.ok, doctor.format_text()
        assert len(doctor.registered_tools) == 12
        manager = PluginManager()
        manager.discover_and_load()

        def call(action, **arguments):
            value = registry.dispatch('mailbridge_' + action, arguments)
            result = json.loads(value) if isinstance(value, str) else value
            assert result.get('ok'), result
            return result

        def wait(tid):
            end = time.monotonic() + 30
            while time.monotonic() < end:
                result = call('status', tid=tid)
                if not result['worker_active']:
                    assert result['status'] == 'complete', result
                    return result
                time.sleep(.1)
            raise AssertionError('Hermes worker timeout')

        try:
            assert call('gmail_connection_status')['connections'] == []
            assert call('gmail_connect')['status'] == 'setup_required'
            # Real credentialed_provider_request inside an installed worker:
            # declared client, no grant, no Google network request or browser flow.
            os.environ['MAILBRIDGE_GOOGLE_CLIENT_ID'] = 'fictional-client'
            os.environ['MAILBRIDGE_GOOGLE_CLIENT_SECRET'] = 'fictional-client-secret'
            assert call('gmail_connect')['status'] == 'sign_in_required'
            del os.environ['MAILBRIDGE_GOOGLE_CLIENT_ID']
            del os.environ['MAILBRIDGE_GOOGLE_CLIENT_SECRET']
            providers = call('providers')['providers']
            assert len(providers) == 38
            tid = call('plan', mode='demo', scenario='recovery')['job_id']
            wait(tid)
            call('start', tid=tid)
            report = wait(tid)
            assert report['migration']['covered'] == 14, report
            assert report['migration']['unique_messages'] == 12
            call('verify', tid=tid)
            wait(tid)
            report = call('report', tid=tid)
            assert report['migration']['destination_complete'], report
            assert (home / 'plugin-data/mailbridge/reports' / (tid + '.json')).is_file()
            print(json.dumps({'doctor': 'pass', 'registered_tools': len(doctor.registered_tools),
                'catalog_entries': len(providers), 'real_registry_dispatch': 'pass', 'real_hermes_oauth_boundary': 'pass: missing grant fails closed',
                'synthetic_demo': report, 'live_account_access': False}, indent=2))
        finally:
            manager.unload()


if __name__ == '__main__':
    main()
