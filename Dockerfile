# Bruting Calls — shared-server image.
#   docker build -t bruting-calls .
#   docker run -p 8000:8000 bruting-calls                       # Shiny for Python (default)
#   docker run -p 8000:8000 -e APP=r bruting-calls              # Shiny for R
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 ffmpeg r-base r-cran-shiny r-cran-bslib r-cran-ggplot2 r-cran-dplyr \
        r-cran-dt r-cran-jsonlite r-cran-processx r-cran-htmltools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "from birdnetlib.analyzer import Analyzer; Analyzer()"   # bake the model files into the image

COPY app.py app.R install.R DESCRIPTION LICENSE CITATION.cff ./
COPY www ./www
COPY demo ./demo
RUN Rscript install.R

ENV BRUTING_CACHE=/tmp/bruting-cache \
    BRUTING_CACHE_GB=20 \
    BRUTING_MAX_JOBS=2 \
    BRUTING_PYTHON=/usr/local/bin/python \
    APP=python
EXPOSE 8000
CMD ["sh", "-c", "if [ \"$APP\" = r ]; then Rscript -e \"shiny::runApp('app.R', host='0.0.0.0', port=8000)\"; else shiny run app.py --host 0.0.0.0 --port 8000; fi"]
