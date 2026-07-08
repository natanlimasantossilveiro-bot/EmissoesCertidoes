from setuptools import setup, find_packages

setup(
    name="certidoes-core",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "pika>=1.3.0",       # publicação (Gateway) — conexão síncrona de vida curta
        "aio-pika>=9.4.0",   # consumo (workers) — precisa manter heartbeat durante automação longa
        "sqlalchemy>=2.0.0",
        "pymysql>=1.1.0",
        "cryptography>=42.0.0",  # exigido pelo pymysql para o auth caching_sha2_password do MySQL 8
        "boto3>=1.34.0",  # só necessário se STORAGE_BACKEND=s3
    ],
)
