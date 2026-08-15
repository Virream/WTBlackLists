# -*- coding: utf-8 -*-
"""默认共享仓库预置逻辑验证。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.settings import DEFAULT_REPO, AppSettings

d = tempfile.mkdtemp()
p = os.path.join(d, "config.json")

# 1. 首次(无配置文件): fetch/audit 预置默认仓库
s = AppSettings(p)
assert s.fetch_servers and s.fetch_servers[0]["url"] == DEFAULT_REPO, s.fetch_servers
assert s.audit_servers and s.audit_servers[0]["url"] == DEFAULT_REPO, s.audit_servers
print("首次预置默认仓库 OK")

# 2. 用户删除 + save + 重载: 不再自动加回
s.fetch_servers.clear()
s.audit_servers.clear()
s.save()
s2 = AppSettings(p)
assert s2.fetch_servers == [] and s2.audit_servers == [], "删除后不应自动加回"
print("删除后不自动加回 OK")

# 3. 用户更换为自己的仓库: 保留
s3 = AppSettings(p)
s3.fetch_servers.append({
    "url": "https://github.com/other/MyList.git", "platform": "github", "name": "我的",
})
s3.save()
s4 = AppSettings(p)
assert s4.fetch_servers[0]["url"] == "https://github.com/other/MyList.git"
print("更换仓库保留 OK")

print("DEFAULT REPO TEST PASSED")
