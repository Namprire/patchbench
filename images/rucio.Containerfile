# Fixed offline pytest environment for the common-unit pilot; no service credentials.
FROM docker.io/library/python:3.11-slim
RUN pip install --no-cache-dir pytest==8.3.5 requests==2.32.3 typing_extensions==4.12.2 packaging==24.2
# Declared Rucio dependencies needed to collect schema/policy tests locally.
RUN pip install --no-cache-dir jsonschema==4.25.1 SQLAlchemy==2.0.51 alembic==1.16.5 \
    dogpile.cache==1.2.2 rich==14.2.0 PyYAML==6.0.3 prometheus_client==0.25.0
RUN mkdir -p /opt/rucio/etc && printf '[common]\nmulti_vo = false\n[policy]\npermission = generic\n[database]\ndefault = sqlite://\n' > /opt/rucio/etc/rucio.cfg
ENV PYTHONPATH=/workspace/lib:/workspace RUCIO_CLIENT_MODE=1
WORKDIR /workspace
CMD ["python", "-m", "pytest", "--version"]
