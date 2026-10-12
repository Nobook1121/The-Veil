# 控制台命令

服务器启动后，监听窗口会显示 `>` 提示符。网站管理员可以在该窗口输入站点管理命令。

## 语法

- 命令按空格分隔参数。
- 参数包含空格时使用英文双引号，例如 `"user name"`。
- 输入 `help` 查看当前可用命令。

## 用户权限

```text
user promote <username-or-id> [USER|ADMIN|OWNER]
```

将指定用户调整为目标权限。省略目标权限时默认提升为 `ADMIN`。

示例：

```text
user promote alice
user promote alice ADMIN
user promote 1 OWNER
```

也可使用 `user demote <username-or-id> [USER|ADMIN]` 降级，省略目标权限时默认降为 `USER`。

## 强制用户修改密码

```text
user reset-password <username-or-id>
```

将指定用户的密码重置为一个随机临时密码，并把账号标记为「必须修改密码」：

- 该用户所有活跃登录会话会被立即撤销；
- 用户需使用打印到控制台的临时密码重新登录；
- 登录后会被强制要求设置新密码，修改成功前无法继续使用其他功能。

示例：

```text
user reset-password alice
user reset-password 1
```

执行后控制台会输出形如 `Temporary password for alice: Xxxx1234 ...` 的临时密码，请妥善转交给用户。

## AI 用量与成本

```text
ai usage [days]
```

汇总最近 N 天（默认 1 天，范围 1-90）的 AI 用量，并按角色与输入 token 分段打印，用于判断成本花在哪里：

- **总览**：请求次数、输入 token、输出 token、总计，以及平均缓存命中率；
- **按角色（role）**：KP、摘要等不同角色的用量分布；
- **输入分段**：把输入 token 粗估拆分为规则、剧本静态层、场景静态层、知识库、房间状态、历史、玩家输入等段，便于定位「静态前缀占了多大部分」这类问题。

分段 token 为本地粗估（CJK 约 1 token/字，其余约 1/4 字符），真实计费仍以上游 `usage` 为准。

示例：

```text
ai usage
ai usage 7
```
