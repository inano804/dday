# 분산일 대시보드

KOSPI, KOSDAQ, NASDAQ, S&P 500, Dow Jones의 최근 1년 일봉을 조회해 분산일을 표시하는 로컬 웹 프로그램입니다.

분산일 기준은 "전 거래일 대비 종가 0.2% 이상 하락 + 거래량 증가"입니다.

## 실행

```bash
python3 app.py
```

브라우저에서 아래 주소를 엽니다.

```text
http://127.0.0.1:8000
```

## 구성

- `docs/design.md`: 계산 기준과 화면 구성
- `app.py`: 데이터 조회 API와 웹 대시보드

## 데이터 출처

Yahoo Finance chart API를 사용합니다. 네트워크 연결이 필요합니다.
