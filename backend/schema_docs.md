# Fin-Us Backend Database Schema

이 문서는 SQLModel을 사용하여 정의된 SQLite 데이터베이스 스키마를 시각화합니다.

## ER Diagram

```mermaid
erDiagram
    Portfolio {
        int id PK
        string stock_code "종목 코드"
        string stock_name "종목명"
        int quantity "보유 수량"
        float avg_price "평균 매입가"
        float current_price "현재가"
        datetime updated_at "수정 일시"
    }
    TradeHistory {
        int id PK
        string stock_code "종목 코드"
        string stock_name "종목명"
        string trade_type "매매 구분 (BUY/SELL)"
        int quantity "매매 수량"
        float price "매매 단가"
        datetime trade_date "매매 일시"
    }
    AgentReport {
        int id PK
        string stock_code "분석 종목 코드"
        string stock_name "분석 종목명"
        string provider "LLM 제공자"
        string summary "분석 요약"
        string decision "투자 결정 (BUY/SELL/HOLD)"
        float confidence_score "신뢰도 점수"
        string reason "투자 결정 근거"
        datetime created_at "생성 일시"
    }
    Diary {
        int id PK
        string title "일지 제목"
        string content "일지 내용"
        datetime created_at "작성 일시"
    }
```

## 테이블 설명

- **Portfolio**: 사용자의 현재 주식 잔고 정보를 관리합니다. 한국투자증권(KIS) API 데이터를 기준으로 동기화됩니다.
- **TradeHistory**: 사용자의 매매 이력을 기록합니다.
- **AgentReport**: AI 에이전트가 생성한 종목 분석 결과 및 투자 의견을 저장합니다.
- **Diary**: 사용자의 개인적인 투자 생각이나 일지를 기록합니다.
