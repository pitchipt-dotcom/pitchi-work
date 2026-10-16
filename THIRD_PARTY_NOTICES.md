# 第三者依存と由来

本体ライセンスはLICENSING.mdのとおり承認待ちです。以下は依存のmetadata表示です。ライブラリ本体やモデルweightsを同梱しません。

| Package | Version | Upstream license metadata |
|---|---|---|
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anthropic](https://pypi.org/project/anthropic/0.125.0/) | 0.125.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.15.1/) | 4.15.1 | MIT |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | 2026.7.22 | MPL-2.0 |
| [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | BSD-3-Clause (upstream LICENSE.txt) |
| [distro](https://pypi.org/project/distro/1.9.0/) | 1.9.0 | Apache License, Version 2.0 |
| [docstring-parser](https://pypi.org/project/docstring-parser/0.18.0/) | 0.18.0 | MIT |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [httpcore](https://pypi.org/project/httpcore/1.0.9/) | 1.0.9 | BSD-3-Clause |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| [idna](https://pypi.org/project/idna/3.20/) | 3.20 | BSD-3-Clause |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.1/) | 2.3.1 | MIT |
| [jiter](https://pypi.org/project/jiter/0.17.0/) | 0.17.0 | MIT |
| [openai](https://pypi.org/project/openai/2.54.0/) | 2.54.0 | Apache-2.0 |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pillow](https://pypi.org/project/pillow/12.3.0/) | 12.3.0 | MIT-CMU |
| [pluggy](https://pypi.org/project/pluggy/1.7.0/) | 1.7.0 | MIT |
| [pydantic](https://pypi.org/project/pydantic/2.14.1/) | 2.14.1 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.50.1/) | 2.50.1 | MIT |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | MIT |
| [sniffio](https://pypi.org/project/sniffio/1.3.1/) | 1.3.1 | MIT OR Apache-2.0 |
| [tqdm](https://pypi.org/project/tqdm/4.70.1/) | 4.70.1 | MPL-2.0 AND MIT |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |

依存を再配布する場合は各配布物に付属するライセンス全文とNOTICEの条件を確認してください。
自作Host/adapterと、接続先サービス・モデル・付属workflowの条件は別です。
旧プロジェクトから抽出した自作部分を含むため、権利者による由来・寄与の確認はPublic切替前の条件です。
テストの書誌fixtureは公開タイトル/公式リンク等のメタデータだけです。資料本文・個人蔵書・運用DBではありません。
[モデル・外部ソフトの条件](docs/MODEL_LICENSES.md)も参照してください。

ColoramaのPyPI metadataは未指定だったため、[upstream LICENSE.txt](https://github.com/tartley/colorama/blob/master/LICENSE.txt)でBSD-3-Clauseの条件を確認しました。
