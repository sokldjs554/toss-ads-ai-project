# 공개 배포 HTTP 확인

확인 완료 시각: 2026-10-21T02:25:28.127464+00:00

결과: **passed**. 인증서·호스트명 검증을 사용하는 Python 기본 TLS 설정으로 공개 주소에 GET 요청했습니다. TLS 검증 생략은 사용하지 않았습니다. 실제 브라우저 조작이나 모델 API 호출은 이 검사의 범위가 아닙니다.

[기계 판독 원본](receipt.json)에 응답 헤더, 실패 요청, 전체 해시 대조를 기록했습니다.

## 받은 공개 파일

| 주소 | HTTP | SHA-256 |
| --- | --- | --- |
| [https://sokldjs554.github.io/toss-ads-ai-project/](https://sokldjs554.github.io/toss-ads-ai-project/) | 200 | `a48b51e71619ac18e3bcedd3cfa9626493051e3e13c0db71a2a8227510eabb0d` |
| [https://sokldjs554.github.io/toss-ads-ai-project/app/](https://sokldjs554.github.io/toss-ads-ai-project/app/) | 200 | `9083776a80c5927a226c58eacbcb956f95ea250ecadf46efd56234c4f0a324c7` |
| [https://sokldjs554.github.io/toss-ads-ai-project/app/src.zip](https://sokldjs554.github.io/toss-ads-ai-project/app/src.zip) | 200 | `81eb3fd5e0b75eb654bfa146a9532e6842fa6efc9279d4402c90b0f25326dd62` |

## 확인 항목

| 항목 | 결과 |
| --- | --- |
| `landing_http_200` | 완료 |
| `app_http_200` | 완료 |
| `both_runtime_assets_pin_1_12_0` | 완료 |
| `plain_korean_boot_copy` | 완료 |
| `public_and_local_archive_reference_match` | 완료 |
| `archive_http_200` | 완료 |
| `build_info_matches_merged_commit` | 완료 |
| `run3_report_exact_original_bytes` | 완료 |
| `run3_report_completed` | 완료 |
| `packaged_manifest_matches_report_manifest` | 완료 |
| `source_sha256_all_packaged_match` | 완료 |
| `input_sha256_all_packaged_match` | 완료 |
| `frozen_source_exclusions_expected` | 완료 |

예상 병합 커밋: `3bf54e0eeae0f53fc18cf773e82aaa2d96928ba0`. ZIP의 실제 빌드 표시는 `3bf54e0`입니다. 앱 HTML이 참조하는 ZIP은 `https://sokldjs554.github.io/toss-ads-ai-project/app/src.zip`입니다.

저장된 run3 원본의 예상 SHA-256은 `3981f9ab9a82502f02a8e6086e4178289863c8a632317dbb5544710af828fd91`이며 실제 값은 `3981f9ab9a82502f02a8e6086e4178289863c8a632317dbb5544710af828fd91`입니다. 이 보고서가 평가한 소스는 `000bc89ed096f8003d11f40a5c5c6f70e422955c`입니다. 배포 커밋과 모델 평가 커밋은 서로 다른 값이며 같다고 주장하지 않습니다. 포함된 manifest가 보고서에 저장된 manifest와 동일한지 확인한 뒤, 해당 고정 해시와 ZIP 내부 실제 파일을 대조했습니다.

## 포함된 고정 소스·입력 해시

- 고정 소스: 전체 79개 중 ZIP에 포함된 77개, 일치 77개.
- 고정 입력: 전체 18개 중 ZIP에 포함된 6개, 일치 6개.

ZIP에 포함되지 않은 고정 소스는 다음과 같습니다. 이 파일들의 공개 ZIP 해시를 확인했다고 집계하지 않습니다.

- `.github/workflows/live-features.yml`
- `scripts/claim_live_evaluation.py`

ZIP에 포함되지 않은 고정 입력은 다음과 같습니다. 브라우저 묶음에서 제외된 파일의 실제 내용은 이 HTTP 검사로 검증하지 않았습니다.

- `evals/datasets/live_vision_20261020/V01.png`
- `evals/datasets/live_vision_20261020/V02.png`
- `evals/datasets/live_vision_20261020/V03.png`
- `evals/datasets/live_vision_20261020/V04.png`
- `evals/datasets/live_vision_20261020/V05.png`
- `evals/datasets/live_vision_20261020/V06.png`
- `evals/datasets/live_vision_20261020/V07.png`
- `evals/datasets/live_vision_20261020/V08.png`
- `evals/datasets/live_vision_20261020/V09.png`
- `evals/datasets/live_vision_20261020/V10.png`
- `evals/datasets/live_vision_20261020/V11.png`
- `evals/datasets/live_vision_20261020/V12.png`

실패한 HTTP 요청 수: **0**. 기능 동작과 화면 표시는 별도 브라우저 검증 기록을 따릅니다.
