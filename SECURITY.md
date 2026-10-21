# 공개 소스와 비공개 운영 데이터

공개 저장소에는 소스 코드와 예시 설정만 보관합니다. 실제 Discord 토큰,
GitHub 토큰, SSH 키, NAS 접속 주소·계정, 운영 DB, 업로드한 논문·발표 자료,
로그와 백업은 넣지 마세요. 이슈·PR·로그 캡처에도 같은 원칙을 적용합니다.

실제 발표자의 Discord ID·이름이 담긴 `config/rotation.json`과 임시 파일도
Git과 이미지에서 제외합니다. 공개 예시에는 실제 연구실 명단을 쓰지 마세요.

`.gitignore`와 `.dockerignore`는 로컬 설정, 키, DB, PDF/PPT/PPTX, 업로드·백업
디렉터리를 제외합니다. `.env.example`은 공개 가능한 고정 예시만 허용합니다.
이미 추적 중인 파일이나 `git add -f`로 추가한 파일은 ignore만으로 막을 수
없으므로 아래 검사를 함께 사용합니다.

## 개발 환경에서 설정

Python 3.12 이상과 [Gitleaks](https://github.com/gitleaks/gitleaks)의 공식
릴리스 바이너리를 설치합니다. CI에서 사용하는 버전은 **8.30.1**입니다.
릴리스 체크섬도 확인하세요. 각 개발용 clone에서 한 번 실행합니다.

```bash
git config --local core.hooksPath .githooks
git add .
python3 scripts/check_publication.py
gitleaks git --staged --config .gitleaks.toml --redact --ignore-gitleaks-allow .
```

커밋 훅은 Git 인덱스 전체의 비공개 파일·바이너리·예시 환경변수와 staged
변경의 비밀정보를 검사합니다. push 훅은 보내는 브랜치·태그의 전체 이력을
검사합니다. Gitleaks가 없으면 커밋과 push를 중단합니다. NAS에서 clone/pull만
할 때는 훅이나 Gitleaks 설치가 필요하지 않습니다.

GitHub Actions도 전체 공개 이력에 대해 같은 비밀정보 검사와 테스트를
수행합니다. 기본 Gitleaks 규칙 외에 NAS DDNS 주소와 사용자별 홈 경로를
검사합니다. 훅은 새 clone에 자동 설치되지 않으며 우회할 수도 있습니다.
GitHub의 secret scanning/push protection은 지원되는 토큰 패턴을 보완하며,
Actions 검사는 push 후 실행됩니다. 임의의 암호나 모든 개인정보를 자동
판별할 수는 없으므로 diff를 직접 검토하세요.

실제 토큰을 커밋하거나 공개했다면 파일 삭제만으로 해결되지 않습니다.
해당 토큰을 즉시 폐기·재발급하고 이력과 로그에 남은 내용을 정리하세요.
민감한 값 자체를 공개 이슈에 적지 마세요.
