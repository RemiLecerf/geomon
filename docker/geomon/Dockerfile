# Geomon application image — Debian testing (Specifications §2.1).
# First-product image: GDAL + Python geospatial stack. Rust, CUDA and the ML
# libraries (§2.4) are left for a later, heavier image variant.
ARG BASE_IMAGE=debian:testing-slim
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# gdal-bin provides ogr2ogr (OSM PBF -> PostGIS). The Python packages bring their
# own GDAL build (rasterio wheels); they run in separate processes from ogr2ogr.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gdal-bin postgresql-client \
      python3 python3-venv python3-dev build-essential gfortran \
 && rm -rf /var/lib/apt/lists/*

COPY docker/geomon/requirements.txt /tmp/requirements.txt
RUN python3 -m venv $VIRTUAL_ENV \
 && pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY geomon /app/geomon
COPY web /app/web

# Data is never stored in the image: /data is a bind mount from the host (§3.1).
VOLUME ["/data"]
EXPOSE 8000
