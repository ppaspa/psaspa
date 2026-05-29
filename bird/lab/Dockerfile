# Usamos o Ubuntu como base
FROM ubuntu:22.04

# Instalamos as ferramentas de compilação que o BIRD precisa
RUN apt-get update && apt-get install -y \
    build-essential \
    autoconf \
    flex \
    bison \
    libncurses-dev \
    libreadline-dev \
    libssh-dev \
    iproute2 \
    iputils-ping \
    vim \
    git

# Criamos uma pasta para o código
WORKDIR /bird-source
