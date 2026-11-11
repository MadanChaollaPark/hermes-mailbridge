PYTHON ?= python3
.PHONY: test check

test:
	$(PYTHON) -m unittest discover -s tests -v

check: test
	$(PYTHON) -m compileall -q gmail_eml_migration_v2.py pilot mailbridge bridge_plugin hermes_tools.py mailbridge_plugin.py install.py
