.PHONY: help setup example lint test scan security clean
help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup:            ## install dependencies into the active environment
	python3 -m pip install -r requirements.txt

example:          ## run the offline example (no AWS account needed)
	cd examples/offline && python3 generate.py

lint:             ## byte-compile every module
	python3 -m compileall -q etd examples/offline scripts tests

test: lint configs example scan security ## lint, validate configs, render, scan, security
	@python3 -c "import pathlib,sys; \
p=pathlib.Path('examples/offline/out/report.html'); \
h=p.read_text(); \
assert p.stat().st_size > 100_000, 'report suspiciously small'; \
assert h.count('<div') == h.count('</div>'), 'unbalanced markup'; \
assert 'SYNTHETIC' in h, 'sample report must be labelled synthetic'; \
print('ok: report renders, markup balanced, provenance labelled')"

scan:             ## fail if a non-public identifier is present
	python3 scripts/scan_identifiers.py .

configs:          ## every shipped config must parse
	python3 tests/test_configs.py

security:         ## security regression tests (needs `make example` first)
	python3 tests/test_security.py
	python3 tests/test_ops_sync.py

clean:            ## remove generated artifacts
	rm -rf examples/offline/out runs share
	find . -name __pycache__ -type d -exec rm -rf {} +
