# deploy/harden —— 公网迁移加固脚本（阶段二执行，13 §3.3）

本目录在拿到公网 VPS 后填充，逐项对应 13 §3.3 checklist：

1. `firewall.sh` —— 入站仅放行 运维SSH(高位)/2222/4000，其余全 deny
2. `egress-deny.sh` —— docker 网络出站 default-deny；宿主机出站仅运维白名单
3. `sshd-hardening.sh` —— 禁 root 密码 / 仅密钥 / fail2ban
4. `retention.sh` —— IP /24 截断校验 + 加盐哈希 + 90 天留存清理
5. `kill-switch.sh` —— 异常出站/上传/横扫信号 → 停服

阶段一（内网）**不需要**这些脚本。
