FROM python:3.11-slim@sha256:a2bc8c35469b6fe37735f7c4dae39049470b2ce068e73f799c02452de31d24c6
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown app:app /data
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 torchvision==0.22.1
COPY pyproject.toml ./
# Install dependencies independently of application code; pyproject.toml remains the source of truth.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib,subprocess; dependencies=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; subprocess.check_call(['pip','install',*dependencies])"
# Bind the source only for this build step, so the image contains one installed copy of the app.
RUN --mount=type=bind,source=frameseek,target=/app/frameseek,rw \
    --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps --no-build-isolation . && rm -rf /app/build /app/frameseek.egg-info
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live')"
CMD ["frameseek", "serve", "--host", "0.0.0.0", "--port", "8000"]
