# 운천 FC 공식 홈페이지 — 공개 배포판 (Python)

이 프로젝트는 기존 운천 FC 홈페이지 디자인·엠블럼·입단지원서 PDF를 유지하면서,
누구나 인터넷으로 접속할 수 있는 공개 사이트로 배포하기 위해 준비한 파이썬 프로젝트입니다.

## 가장 간단한 공개 배포: GitHub + Render

1. https://github.com/new 에서 새 저장소(repository)를 만듭니다. (예: uncheon-fc)
2. 압축을 푼 이 폴더의 파일들을 GitHub 저장소 최상위에 업로드하고 커밋합니다.
   - 필수: uncheon_fc.py / webapp.py / site_data.json / requirements.txt
   - render.yaml도 포함하면 Render Blueprint에서 설정을 자동으로 적용할 수 있습니다.
3. https://dashboard.render.com/ 에 로그인합니다 (GitHub 계정 연결).
4. New > Web Service > GitHub 저장소 선택.
5. 다음과 같이 설정합니다.
   - Language: Python 3
   - Build Command: pip install -r requirements.txt
   - Start Command: gunicorn --workers 2 --bind 0.0.0.0:$PORT webapp:app
   - Instance: Free (테스트/취미용; 제한사항 있음)
6. 배포에 성공하면 Render 대시보드에 **실제** HTTPS URL이 발급됩니다.
   보통 https://<서비스이름>.onrender.com/ 형태이지만, 실제 주소는 대시보드에 표시된 주소를 확인하세요.
   주소를 미리 임의로 지정하거나 점유를 보장할 수는 없습니다.
7. 원하는 자체 도메인을 구매했다면 Render > Settings > Custom Domains에서 연결한 뒤
   Render 서비스 Environment에 PUBLIC_SITE_URL=https://도메인명 형태로 등록하세요.
   자체 도메인 설정 전에는 RENDER_EXTERNAL_URL 변수를 Render가 자동 제공하므로 별도 지정이 불필요합니다.

※ 개발용 로컬 주소 http://127.0.0.1:8000은 웹사이트 주소가 아닙니다.
※ Render Free 서비스는 사용이 없으면 일시 정지되고 첫 접속이 느릴 수 있습니다.
※ 공개 서버에 사용자 개인정보를 수집·저장하는 기능은 없습니다.
※ 현재 홈페이지만 있고, 아직 사실 확인되지 않은 선수/경기 결과는 등록하지 않았습니다.

## 검색엔진 수집 준비

공개 배포하면 아래 페이지가 자동으로 제공됩니다.
- https://실제-사이트-주소/robots.txt
- https://실제-사이트-주소/sitemap.xml
- 첫 페이지: 검색엔진용 title / description / canonical / Open Graph 메타태그

다음 과정은 사이트 소유자의 계정 로그인이 필요합니다.

**네이버:** https://searchadvisor.naver.com/
- 웹마스터도구 > 사이트 등록 > 실제 공개 URL 입력
- 사이트 소유확인 > HTML 태그 방법을 택한 뒤 메타태그 content 문자열만 복사
- Render 환경변수 NAVER_SITE_VERIFICATION 에 그 문자열을 입력 후 재배포
- 소유확인 후 사이트맵 `/sitemap.xml` 제출 및 수집/색인 상태 점검

**구글:** https://search.google.com/search-console/
- URL 접두어 방식으로 실제 공개 URL 등록
- HTML 태그 확인 방식의 content 문자열을 복사
- Render 환경변수 GOOGLE_SITE_VERIFICATION 에 입력 후 재배포
- 소유확인 후 Sitemaps 메뉴에서 `sitemap.xml` 제출

**다음:** https://register.search.daum.net/
- 사이트 검색 > 신규등록 > 실제 공개 URL 입력
- 안내에 따라 등록정보 제출 및 심사결과 확인

검색 등록 및 사이트맵 제출이 실제 검색 결과의 노출을 보장하지는 않습니다.

## 변경/관리 방법

- `site_data.json`에서 구단 소개, 공지, 경기 일정 및 선수단 내용을 수정하세요.
- GitHub에서 커밋하면 Render가 새 버전으로 자동 배포하도록 설정할 수 있습니다.
- 무료 Render의 파일 시스템에 웹 관리자 화면으로 업로드하거나 수정하는 방식은 지원하지 않습니다.
- Python 3.9 이상 환경에서 `python3 uncheon_fc.py`를 실행하면 기존 로컬 미리보기도 가능합니다.
- 자체 도메인이나 호스팅 서비스를 바꿔도 필요한 경우 PUBLIC_SITE_URL만 업데이트하면
  canonical, Open Graph, sitemap, robots URL이 함께 변경됩니다.

## 프로젝트 구성

- `uncheon_fc.py`  홈페이지 HTML/CSS 및 기존 구단 이미지/PDF 포함, 로컬 미리보기
- `webapp.py`      공개 배포용 Python WSGI 애플리케이션
- `site_data.json`  구단에서 직접 편집하는 콘텐츠
- `requirements.txt`  Python Gunicorn 서버 설치 목록
- `render.yaml`      Render 배포 설정
- `.gitignore`       Git에 업로드하지 않을 파일 목록

## 실제 배포 후 확인

1. URL을 휴대전화 모바일 데이터에서도 열어보기
2. `/robots.txt`, `/sitemap.xml`, `/join-form.pdf` 열어보기
3. Google/Naver 소유확인 후 색인 상태 점검
4. 선수 이름·사진·연락처 공개 시 당사자의 동의를 받고 개인정보가 들어간 지원서는 공개 저장하지 않기
