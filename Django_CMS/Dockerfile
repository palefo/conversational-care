FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies:
#  - gettext: required by manage.py compilemessages
#  - curl: used to fetch the Tailwind standalone CLI
RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext curl \
    && rm -rf /var/lib/apt/lists/*

# Install the Tailwind CSS standalone CLI (no Node required). The binary bundles
# the forms, typography and container-queries plugins used by the templates.
ARG TAILWIND_VERSION=v3.4.17
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) TW_ARCH=linux-x64 ;; \
         arm64) TW_ARCH=linux-arm64 ;; \
         *) echo "Unsupported arch: $ARCH" && exit 1 ;; \
       esac \
    && curl -sL "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${TW_ARCH}" \
         -o /usr/local/bin/tailwindcss \
    && chmod +x /usr/local/bin/tailwindcss

# Install pip and project requirements
# We copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Copy the rest of the project code into the container
COPY . .

# Build the Tailwind stylesheet into the image (used in production images where
# the source tree is not bind-mounted). In dev the entrypoint rebuilds it into
# the mounted volume on start.
RUN tailwindcss \
      -c tailwind/tailwind.config.js \
      -i tailwind/input.css \
      -o ConvAI/static/app/css/tailwind.css \
      --minify

# Entrypoint rebuilds Tailwind CSS on start, then runs the CMD.
# Invoked via `sh` so it works even when the dev bind-mount overlays a
# non-executable copy of the script from the host.
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["sh", "/app/entrypoint.sh"]

# Expose the port the app runs on
EXPOSE 8000

# Specify the default command to run when starting the container
# We use 0.0.0.0 to make the server accessible from outside the container
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]