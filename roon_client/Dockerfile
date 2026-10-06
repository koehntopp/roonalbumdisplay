FROM python:3.13-slim

# Otherwise stdout is block-buffered when not attached to a TTY, and
# `docker logs` shows nothing until a lot of output accumulates - the same
# pitfall already hit (and documented in AGENTS.md) running this ad hoc in
# a background shell during development.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir roonapi requests Pillow

COPY roon_to_matrix.py .

# Credentials (roon_core_id.txt, roon_token.txt) are mounted at runtime
# (from an NFS volume on the NAS), not baked into the image - see
# compose.yaml and AGENTS.md ("Docker deployment").
ENTRYPOINT ["python", "roon_to_matrix.py"]
