.PHONY: build up down logs gateway-logs status restart shell health clean rebuild dev \
        hermes-up hermes-down openclaw-up openclaw-down postgres-up postgres-down

# Overridable from the environment, e.g.
#   A2A_GATEWAY_CONTAINER_NAME=my-gateway make shell
#   CONTAINER_PORT=9000 make health
# Note: make does NOT read .env — export the variable if you changed it there.
A2A_GATEWAY_CONTAINER_NAME ?= a2a-gateway
CONTAINER_PORT             ?= 8765

# Build the Docker image
build:
	docker compose build

# Start the gateway in the background
up:
	docker compose up -d

# Build and start with live logs (foreground)
dev:
	docker compose up --build

# Stop and remove containers
down:
	docker compose down

# Tail combined logs (gateway + optional hermes / openclaw / postgres)
logs:
	docker compose logs -f

# Tail just the gateway process log inside the container
gateway-logs:
	docker exec $(A2A_GATEWAY_CONTAINER_NAME) supervisorctl tail -f silvaengine-gateway

# Supervisor process status
status:
	docker exec $(A2A_GATEWAY_CONTAINER_NAME) supervisorctl status

# Restart the gateway process without rebuilding the container
restart:
	docker exec $(A2A_GATEWAY_CONTAINER_NAME) supervisorctl restart silvaengine-gateway

# Open a shell in the gateway container
shell:
	docker exec -it $(A2A_GATEWAY_CONTAINER_NAME) /bin/bash

# Hit the public health endpoint
health:
	curl -f http://localhost:$(CONTAINER_PORT)/health

# Stop containers and drop volumes + dangling images
clean:
	docker compose down -v
	docker image prune -f

# Full rebuild from scratch
rebuild: clean build up

# Bring up the bundled Hermes Agent sibling service (profile: hermes)
hermes-up:
	docker compose --profile hermes up -d hermes

# Stop the bundled Hermes sibling (gateway stays up)
hermes-down:
	docker compose --profile hermes stop hermes

# Bring up the bundled OpenClaw sibling service (profile: openclaw)
openclaw-up:
	docker compose --profile openclaw up -d openclaw

# Stop the bundled OpenClaw sibling (gateway stays up)
openclaw-down:
	docker compose --profile openclaw stop openclaw

# Bring up the bundled PostgreSQL sibling service (profile: postgres)
postgres-up:
	docker compose --profile postgres up -d postgres

# Stop the bundled PostgreSQL sibling (gateway stays up)
postgres-down:
	docker compose --profile postgres stop postgres
