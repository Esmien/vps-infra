# Песочница для Open WebUI: не-root, без доступа к хосту.
# База — тот же open-terminal, что и рабочий, но с arduino-cli и AVR-ядром.
FROM ghcr.io/open-webui/open-terminal:latest

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# arduino-cli (latest stable) в /usr/local/bin
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh \
 && arduino-cli version

ENV ARDUINO_DIRECTORIES_DATA=/home/user/.arduino15 \
    ARDUINO_DIRECTORIES_USER=/home/user/Arduino

# AVR-ядро и стандартные библиотеки раскладываем в образ, чтобы компиляция работала без сети
RUN arduino-cli config init --overwrite \
 && arduino-cli core update-index \
 && arduino-cli core install arduino:avr \
 && arduino-cli lib update-index

RUN mkdir -p /workspace /home/user/Arduino && chown -R 1000:1000 /workspace /home/user

USER user
WORKDIR /workspace
