task "memcached" {
  driver = "docker"

  lifecycle {
    hook    = "prestart"
    sidecar = true
  }

  config {
    image        = "memcached:1.6-alpine"
    network_mode = "bridge"
    ports        = ["memcached"]
    command      = "memcached"
    args         = ["-m", "1024", "-l", "0.0.0.0"]
  }

  resources {
    memory = 1024
  }
}
