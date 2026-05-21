task "memcached" {
  driver = "docker"

  lifecycle {
    hook    = "prestart"
    sidecar = true
  }

  config {
    image   = "memcached:1.6-alpine"
    command = "memcached"
    args    = ["-m", "1024"]
  }

  resources {
    memory = 1024
  }
}
