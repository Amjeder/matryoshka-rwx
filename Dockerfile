FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN install -d -m 755 /app
COPY --chmod=644 selector.py precision.py coverage.json.gz sources.zip /app/
CMD ["python", "/app/selector.py", "--strategy", "all"]
