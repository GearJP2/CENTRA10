.PHONY: test render plan
test:
	python3 -m unittest discover -s tests -v

render:
	python3 -m experiments.render --config config.local.json

plan:
	python3 -m experiments.run --config config.local.json
