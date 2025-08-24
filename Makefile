# Use the venv Python by default
PYTHON = .venv/bin/python
PELICAN = .venv/bin/pelican

# Default: show help
help:
	@echo "make venv    -> create virtualenv & install deps"
	@echo "make dev     -> run Pelican dev server on :8000"
	@echo "make post    -> generate a new hourly game post"
	@echo "make build   -> build site (output/ folder)"

# Create .venv and install requirements
venv:
	python3 -m venv .venv
	. .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

# Run Pelican dev server (autoreload, listen on :8000)
dev:
	. .venv/bin/activate && $(PELICAN) -r -l content -s pelicanconf.py -o output

# Generate a new post (Steam game pick)
post:
	. .venv/bin/activate && $(PYTHON) scripts/run_pipeline.py

# Build site (publish mode, no server)
build:
	. .venv/bin/activate && $(PELICAN) content -s publishconf.py -o output
