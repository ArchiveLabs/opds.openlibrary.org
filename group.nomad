task "memcached" {
  driver = "docker"

  lifecycle {
    hook    = "prestart"
    sidecar = true
  }

  config {
    image   = "memcached:1.6-alpine"
    ports   = ["memcached"]
    command = "memcached"
    args    = ["-m", "1024"]
  }

  resources {
    memory = 1024
  }
}

task "opds" {
  driver = "docker"

  config {
    image = var.IMAGE
    ports = ["http"]
  }

  template {
    data = <<EOF
SENTRY_DSN="${var.SENTRY_DSN}"
EOF
    destination = "secrets/file.env"
    change_mode = "restart"
    env         = true
  }

  env {
    ENVIRONMENT                        = "production"
    WEB_CONCURRENCY                    = "2"
    CACHE_ENABLED                      = "true"
    MEMCACHE_HOST                      = "${NOMAD_IP_memcached}"
    MEMCACHE_PORT                      = "${NOMAD_PORT_memcached}"
    OL_BASE_URL                        = "https://openlibrary.org"
    OPDS_BASE_URL                      = "https://opds.openlibrary.org/opds"
    SENTRY_TRACES_SAMPLE_RATE          = "0.1"
    SENTRY_PROFILE_SESSION_SAMPLE_RATE = "0.1"
  }

  resources {
    memory = 2048
    cpu    = 2000
  }
}
