# stlite 1.12 앱·저장 호환성 확인

이 폴더는 브라우저 Python 런타임 변경에 대한 로컬 확인 자료다. 앱 소스의 저장 로직을 바꾸지 않고 임시 빌드 HTML의 stlite 버전만 바꾸어 비교했다. 유료 API 호출과 공개 배포 확인은 포함하지 않는다. 유휴 중단 재현과 반복 화면 전환은 별도 [런타임 증거](../stlite-runtime/)에 있다.

## 서로 다른 실행의 결과를 구분

| 실행 | 결과 | 해석 |
| --- | --- | --- |
| [compatibility/result.json](compatibility/result.json) | 35개 확인까지 통과한 뒤 selector 대기 시간 초과 | 전체 통과가 아닌 **부분 실행 / 실패**다. 최초 앱·예시·업종 전환·성과 보고서·사례·품질 화면과 최소 저장 UI까지 확인했지만, 전체 앱의 표에서 숨겨진 접근성 셀을 visible로 기다렸다. |
| [storage-compatibility/result.json](storage-compatibility/result.json) | 1.11.0 12/12, 1.12.0 12/12, 종료 코드 0 | 남은 저장 복원을 가시적 대기 수·실제 광고 배정 폼·IndexedDB 저장 내용·새로고침 후 복원으로 독립 확인했다. |

부분 실행의 `status: failed`, 예외, 캡처, 콘솔 및 요청 오류는 수정하지 않았다. 실패 메시지에는 광고 제목을 가진 `role="gridcell"`이 있으나 숨겨진 상태였다는 내용이 남아 있다. 이어서 수행한 저장 비교는 그 셀을 단독 근거로 삼지 않는다. **화면의 대기 1건, 실제 광고 문구, 담당자 이름, 저장 완료 응답과 새로고침 후 같은 작업 복원**을 함께 확인한다. 두 실행의 개수를 하나의 완주한 검사 수처럼 합치지 않는다.

`compatibility/attempt1-selector/`와 `compatibility/attempt2-navigation-selector/`도 초기 검사 도구 오류를 보존한다. 업종 값은 selectbox의 실제 입력 값으로, 화면 이동은 접근성 역할 `radio`인 버튼으로 확인해야 했다. 이 selector 수정 과정에서 제품 코드는 바꾸지 않았다. `compatibility/`의 품질 화면은 **run2** 결과이며 최종 run3 화면은 [browser-run3](../browser-run3/)에 별도로 있다.

## 저장과 콘솔 요청 실패의 구분

두 런타임 모두 같은 절차에서 `stlite.invalid` 진단 요청 실패 8회, 콘솔 오류 24개가 관측됐다. **콘솔 오류가 없었다는 결과가 아니다.** CDP 호출 스택은 `checkSourceUrlResponse`의 외부 URL 사전 확인에서 실패한 요청임을 보여준다. 실제 저장 컴포넌트는 별도 `srcDoc` iframe으로 실행됐고, 저장 완료·IndexedDB revision 증가·본 앱 복원을 확인했다.

1.11과 1.12에서 진단 요청 실패와 저장 동작이 동일하므로, 이번 비교에서 1.12로 인해 새로 생긴 저장 기능 결함은 관측하지 않았다. 프레임워크의 진단 요청 오류 자체는 남아 있다. 자세한 호출 스택·프레임·snapshot과 제한은 [저장 비교 설명](storage-compatibility/README.md)에 있다. 로그를 숨기거나 실패한 fetch를 성공으로 바꾸는 코드는 추가하지 않았다.
