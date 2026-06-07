# Use a Python slim base image for a lightweight container
FROM python:3.11-slim

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set the working directory in the container
WORKDIR /app

# Copy the package configuration and source files
COPY pyproject.toml README.md *.py ./
COPY templates/ ./templates/
COPY AGENTS.md ./

# Create default vault path and set env var
ENV SECOND_BRAIN_PATH=/app/vault
RUN mkdir -p /app/vault

# Install dependencies using uv
RUN uv pip install --system .

# Expose port (if HTTP/SSE transport is used, though stdio is default for MCP)
EXPOSE 9100

# Run the server using stdio transport by default
ENTRYPOINT ["python", "server.py", "--transport", "stdio"]
