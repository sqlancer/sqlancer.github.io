# Convenience targets for the impact pipeline. See tools/impact/README.md.

.PHONY: help validate stats plots build test collect collect-full serve

help:
	@echo "validate     - validate _data/impact against the JSON Schemas"
	@echo "stats        - regenerate _data/impact/stats.json from the records"
	@echo "plots        - regenerate the charts in _includes/impact/plots"
	@echo "build        - stats + plots + validate"
	@echo "test         - run the impact test suite"
	@echo "collect      - incremental collection run (writes _data/impact)"
	@echo "collect-full - periodic full reconciliation"
	@echo "serve        - build and serve the website locally"

validate:
	python3 -m tools.impact.run validate

stats:
	python3 -m tools.impact.run stats

plots:
	python3 -m tools.impact.run plots

build:
	python3 -m tools.impact.run build

test:
	python3 -m unittest discover -s tools/impact/tests -t . -v

collect:
	python3 -m tools.impact.run collect --report .cache/impact/last-run.json

collect-full:
	python3 -m tools.impact.run collect --full --report .cache/impact/last-run.json

serve:
	bundle exec jekyll serve
