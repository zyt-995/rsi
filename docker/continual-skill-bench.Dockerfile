FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY csb-requirements.txt /tmp/csb-requirements.txt
RUN pip install --no-cache-dir -r /tmp/csb-requirements.txt

# Harbor source and benchmark tasks are mounted at the identical host path.
# This is required because the sibling Docker daemon resolves bind paths on host.
ENTRYPOINT ["python", "-m", "harbor.cli.main"]
