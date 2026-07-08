FROM python:3.12-slim
WORKDIR /srv
COPY ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ui/app.py .
EXPOSE 8080
CMD ["streamlit", "run", "app.py", "--server.port", "8080", "--server.address", "0.0.0.0"]
