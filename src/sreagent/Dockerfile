# SRE agent image. python:3.12-slim (glibc), not Alpine: every dependency
# installs from a prebuilt wheel, so no compiler is needed in either stage.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS base

FROM base AS builder

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /install /usr/local

# Run as an unprivileged user; the agent needs no root and no writable files
RUN useradd --system --uid 10001 --no-create-home sreagent
WORKDIR /app
COPY --chown=root:root *.py ./
USER 10001

EXPOSE 8080
ENTRYPOINT ["python", "server.py"]
