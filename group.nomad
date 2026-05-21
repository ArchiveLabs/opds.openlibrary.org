task "memcached" {
  driver = "podman"

  lifecycle {
    hook    = "prestart"
    sidecar = true
  }

  config {
    image        = "memcached:1.6-alpine"
    network_mode = "host"
    command      = "memcached"
    args         = ["-m", "1024", "-l", "127.0.0.1"]
  }

  resources {
    memory = 1024
  }
}
