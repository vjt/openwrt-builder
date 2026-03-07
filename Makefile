REMOTE     ?= user@host
REMOTE_DIR ?= /opt/openwrt-builder

deploy:
	git push
	ssh $(REMOTE) "cd $(REMOTE_DIR) && git pull && docker compose up -d --build"
