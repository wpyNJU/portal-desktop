# Third-party assets

The application source uses the MIT license in [LICENSE](LICENSE).

## Noto Sans SC

`mobile/voice-ui.woff2` and `mobile/voice-sans.woff2` are the font assets used by the existing mobile deployment, copied without changing their bytes. Both identify themselves as **Noto Sans SC Regular** in the font metadata, with this notice:

> © 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.

These fonts are distributed under the **SIL Open Font License 1.1**, reproduced in [licenses/NotoSansSC-OFL.txt](licenses/NotoSansSC-OFL.txt). The fonts retain their own license rather than the application's MIT license.

Upstream license: [Google Fonts / Noto Sans SC](https://github.com/google/fonts/blob/main/ofl/notosanssc/OFL.txt).

| Asset | SHA-256 |
| --- | --- |
| voice-ui.woff2 | `2c75e03b7921485509660fce58d1f7dda43a6f4f4b00aa06d34ac82ff616efdf` |
| voice-sans.woff2 | `9e9fc8d4b6d48a30bd155de774d32f5367818f02f0f17ea4ef4c8823ef0de8ac` |

Private recordings, API keys, user links, profiles, task databases and logs are not included. Python dependencies are installed from `requirements.txt`; they are not vendored in this archive and retain their respective licenses.
