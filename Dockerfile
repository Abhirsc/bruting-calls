# Bruting Calls — container image (Hugging Face Docker Space, or any Docker host).
#
#   docker build -t bruting-calls .                                  # Python app (default, smaller)
#   docker run -p 8000:8000 bruting-calls
#
#   docker build --build-arg WITH_R=true -t bruting-calls-r .         # also install R + app.R
#   docker run -p 8000:8000 -e APP=r bruting-calls-r
#
# Never set BRUTING_ALLOW_STOP on a shared/hosted container.
FROM python:3.12-slim-bookworm

ARG WITH_R=false
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
    && if [ "$WITH_R" = "true" ]; then apt-get install -y --no-install-recommends r-base-core; fi \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces run the container as user 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "from birdnetlib.analyzer import Analyzer; Analyzer()"   # fail the build early if the model can't load

# R packages as prebuilt Linux binaries from Posit Package Manager (Debian's own r-cran-* are too old for app.R)
COPY --chown=user install.R DESCRIPTION ./
RUN if [ "$WITH_R" = "true" ]; then \
      Rscript -e 'options(repos = c(CRAN = "https://packagemanager.posit.co/cran/__linux__/bookworm/latest")); source("install.R")'; \
    fi

COPY --chown=user app.py app.R LICENSE CITATION.cff README.md ./
COPY --chown=user www ./www
COPY --chown=user demo ./demo
USER user

ENV BRUTING_CACHE=/tmp/bruting-cache \
    BRUTING_CACHE_GB=20 \
    BRUTING_MAX_JOBS=2 \
    BRUTING_PYTHON=/usr/local/bin/python \
    APP=python
EXPOSE 8000
CMD ["sh", "-c", "if [ \"$APP\" = r ]; then Rscript -e \"shiny::runApp('app.R', host='0.0.0.0', port=8000)\"; else shiny run app.py --host 0.0.0.0 --port 8000; fi"]
