.PHONY: test packages packages-all bundle bundle-generic bundle-windows bundle-windows-generic windows-exe

PYTHON_WINDOWS_RUNTIME ?= /tmp/codex-installer-deps/python-3.13.13-embed-amd64.zip
MAKENSIS ?= /tmp/codex-installer-deps/nsis-root/usr/bin/makensis
NSIS_DIR ?= /tmp/codex-installer-deps/nsis-root/usr/share/nsis

test:
	python3 -m unittest discover -s tests -v
	python3 -m py_compile server.py account_registry.py usage_aggregation.py usage_analytics.py collector.py collector_windows_machine.py tools/build_collector_bundle.py tools/build_windows_bundle.py tools/build_windows_exe.py
	node --check static/usage-view.js
	node --check static/analytics-view.js
	node --check static/app.js
	node --check static/accounts-view.js
	node --test tests/test_charts.js tests/test_analytics_view.js

bundle:
	python3 tools/build_collector_bundle.py

bundle-generic:
	python3 tools/build_collector_bundle.py --without-token

bundle-windows:
	python3 tools/build_windows_bundle.py

bundle-windows-generic:
	python3 tools/build_windows_bundle.py --without-token

packages: bundle bundle-windows bundle-generic bundle-windows-generic

windows-exe:
	python3 tools/build_windows_exe.py --python-runtime $(PYTHON_WINDOWS_RUNTIME) --makensis $(MAKENSIS) --nsis-dir $(NSIS_DIR)

packages-all: packages windows-exe
