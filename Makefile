.PHONY: test render plan validate
test:
	python3 -m unittest discover -s tests -v

render:
	python3 -m experiments.render --config config.local.json

plan:
	python3 -m experiments.run --config config.local.json

validate:
	python3 -m experiments.validate
