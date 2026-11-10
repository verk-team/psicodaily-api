FROM python:3.14-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        firebird4.0-server \
        firebird4.0-utils \
        libfbclient2 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# O pacote do Debian deixa o Firebird ouvindo so em localhost, e uma porta publicada pelo Docker
# (DBeaver por tunel SSH) chega pelo IP do container. A 3050 nunca e publicada para a internet.
RUN sed -i 's/^RemoteBindAddress = localhost$/RemoteBindAddress = 0.0.0.0/' /etc/firebird/4.0/firebird.conf \
    && grep -qx 'RemoteBindAddress = 0.0.0.0' /etc/firebird/4.0/firebird.conf

ENV TZ=America/Sao_Paulo

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

CMD ["/usr/local/bin/docker-entrypoint.sh"]