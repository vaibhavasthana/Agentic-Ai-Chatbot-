#!/usr/bin/env bash
set -e

echo "==> Installing Microsoft ODBC Driver 18 for SQL Server..."

# Install prerequisites
apt-get update -qq
apt-get install -y -qq curl apt-transport-https gnupg2

# Add Microsoft repo and install ODBC driver
curl -sSL https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
curl -sSL https://packages.microsoft.com/config/debian/11/prod.list \
    > /etc/apt/sources.list.d/mssql-release.list

apt-get update -qq
ACCEPT_EULA=Y apt-get install -y -qq msodbcsql18 unixodbc-dev

echo "==> ODBC Driver installed successfully"
echo "==> Installing Python dependencies..."

pip install -r requirements.txt

echo "==> Build complete"
