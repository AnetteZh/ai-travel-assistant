.PHONY: up down logs ps install install-agent install-all seed validate test demo clean checks eval golden golden-holdout

# --- Docker: весь проект на http://localhost:8080
up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# --- Локальная разработка Core и Data

install:
	python -m pip install -e .

install-agent:
	python -m pip install -e ".[api,langgraph]"

install-all:
	python -m pip install -e ".[api,langgraph,test]"

seed:
	python scripts/generate_world.py

validate:
	python scripts/validate_world.py

test:
	python -m pytest -q

demo:
	python examples/run_demo.py

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache build src/*.egg-info
# --- Проверки и качество
checks:
	python -m pytest -q tests/test_trip_checks.py tests/test_guardrails.py tests/test_eval.py

eval:
	python -m evals.run_scenarios

golden:
	python -m evals.run_golden --split dev

golden-holdout:
	python -m evals.run_golden --split holdout
