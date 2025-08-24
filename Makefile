# Use the venv Python by default
PYTHON = .venv/bin/python

help:
	@echo "make venv   -> create virtualenv & install deps"
	@echo "make dev    -> run Pelican dev server on :8000"
	@echo "make post   -> generate a new game post"
	@echo "make build  -> build site (publish)"

venv:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

dev:
	. .venv/bin/activate && $(PYTHON) -m pelican -r -l content -s pelicanconf.py -o output

post:
	. .venv/bin/activate && $(PYTHON) scripts/run_pipeline.py

build:
	. .venv/bin/activate && $(PYTHON) -m pelican content -s publishconf.py -o output
