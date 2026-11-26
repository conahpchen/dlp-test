FROM python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libseccomp2 openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 honeynet \
    && useradd --uid 10001 --gid 10001 --no-create-home honeynet

COPY requirements.txt .
RUN --mount=type=secret,id=pip_ca \
    if [ -r /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi \
    && pip install --no-cache-dir -r requirements.txt
COPY agent_runtime agent_runtime
COPY analysis analysis
COPY audit audit
COPY business business
COPY contracts contracts
COPY dws dws
COPY entries entries
COPY generation generation
COPY identity identity
COPY judgment judgment
COPY management management
COPY replay replay
COPY research research
COPY scorer scorer
COPY deploy/harden/network.py deploy/harden/network.py
COPY deploy/state_backup.py deploy/state_backup.py
COPY deploy/docker/init-state.sh deploy/docker/run.sh deploy/docker/
COPY tests/fixtures/world_seed.demo.json tests/fixtures/world_seed.demo.json
RUN chmod -R a+rX /app

USER 10001:10001
ENTRYPOINT ["/bin/sh", "/app/deploy/docker/run.sh"]
