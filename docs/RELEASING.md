# 自动发布

推送与 package.json 版本一致的新标签（例如 v1.1.1）会触发 release.yml：

- Windows x64：Setup.exe 和 ZIP。
- macOS Apple Silicon、Intel：各自的 DMG 和 ZIP。
- 三端构建、测试和安装包检查全部通过后，校验清单与安装包统一发布到 GitHub Release。任一平台失败都不发布不完整版本。

macOS 默认通过显式 PORTAL_DESKTOP_MAC_ADHOC_RELEASE=1 构建临时签名包，不需要 Apple 证书。CI 检查签名完整性、DMG 和安装包校验清单，但这些不是 Apple Developer ID 认证或公证。首次打开可能被 Gatekeeper 拦截，用户需在系统设置“隐私与安全性”中允许打开。应用内自动升级的正式签名验证不适用于此包，须手动下载新版本安装；不降低应用内升级校验。

若恢复正式签名，把上述发布环境变量改为 0，并在 Actions Secrets 配置 APPLE_CERTIFICATE_BASE64 和 APPLE_CERTIFICATE_PASSWORD；证书需匹配 desktop/macos-signing.json 及 Heart Portal 的签名配置。不要提交证书私钥或密码。PORTAL_DESKTOP_MAC_LOCAL_TEST 仍禁止用于版本标签。

先更新 package.json、package-lock.json 的版本及 desktop/RELEASE_NOTES.md，提交并推送后再创建同版本标签。不要移动已有标签。若构建成功但上传中断，可使用 publish-existing.yml，输入原版本标签和构建 run ID 恢复发布。

语音网关代码也在仓库内，但桌面 Release 不会自动部署云端服务。服务器密钥、任务库及日志不随源码或安装包发布。

如果标签推送时 Actions 尚未启用，可在 Build paired client release 页面点击 Run workflow，选择 main，并输入已有标签（如 v1.1.1）。工作流会检出该标签的源码，验证版本一致及版本尚未发布，不移动标签。手动运行失败后可在该运行页面重试失败任务。
