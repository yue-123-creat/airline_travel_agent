# 用国内源拉镜像并 tag 成官方名，保证 compose 与上云一致（均为 Docker 内服务）
$ErrorActionPreference = "Stop"

Write-Host "==> postgres:15-alpine via DaoCloud"
docker pull docker.m.daocloud.io/library/postgres:15-alpine
docker tag docker.m.daocloud.io/library/postgres:15-alpine postgres:15-alpine

Write-Host "==> python:3.12-slim via DaoCloud (for API build)"
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim

Write-Host "==> redis:7-alpine (usually already local; fallback DaoCloud)"
docker pull redis:7-alpine
if ($LASTEXITCODE -ne 0) {
  docker pull docker.m.daocloud.io/library/redis:7-alpine
  docker tag docker.m.daocloud.io/library/redis:7-alpine redis:7-alpine
}

Write-Host "Done. Images:"
docker images postgres:15-alpine
docker images python:3.12-slim
docker images redis:7-alpine
