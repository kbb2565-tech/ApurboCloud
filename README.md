# ApurboCloud

Railway deploy: Dockerfile + start.sh (gunicorn + optional Cloudflare Tunnel).
Volume mount: /data (DATA_DIR=/data)

Required env:
- NORMAL_PASSWORD (optional)
- SECRET_KEY
- ADMIN_PASSWORD (default APURBODEV)
- CLOUDFLARE_TUNNEL_TOKEN (optional, Cloudflare Tunnel er jonno)

Cloudflare Tunnel setup:
1. Cloudflare Zero Trust > Networks > Tunnels > Create a tunnel (Cloudflared)
2. Token copy kore Railway Variables e CLOUDFLARE_TUNNEL_TOKEN e rakho
3. Public Hostname: subdomain + domain, Service type HTTP, URL: http://localhost:<PORT>
   (PORT Railway te jeta set kora, default 5000)
