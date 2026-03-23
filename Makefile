SHELL := /bin/sh

.PHONY: test-state

test-state:
	docker compose -f factory/node-32gb/docker-compose.yml up -d --build api
	docker compose -f factory/node-32gb/docker-compose.yml exec -T api python -m unittest tests/test_state_machine.py -v
