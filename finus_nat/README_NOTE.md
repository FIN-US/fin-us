# Fin-Us NAT 설정

## 1. Mem0 self-hosted server 설정

**Prerequisites**

`Docker, Docker compose, OPEN_API_KEY, Port 8888 for API and 3000 for dashboard`

1. `(1) git clone https://github.com/mem0ai/mem0.git`
2. `(2) 해당 레포지토리에서 cp .env.example .env`
3. `(3) .env에서 OPEN_API_KET 필드에 키를 입력하거나 랜덤한 문자열을 입력한다. LLM API KEY는 boostrap한 이후 dashboard에서 설정할 수 있다.`
4. `(4) .env의 JWT_SECRET에 랜덤한 문자열을 삽입한다. (필수)`
5. `(5) cd server && make bootstrap`
6. `(6-1) 컨테이너가 성공적으로 빌드되었다면 터미널에 이메일과 비밀번호가 출력된다. make bootstrap EMAIL=admin@company.com PASSWORD='strong-password' NAME='Admin' 를 사용하여 빌드 이전에 이메일과 비밀번호를 설정할 수 있다.`
7. `(6-2) 만일 정상적으로 빌드가 되지 않는다면 server/.env를 다음과 같이 설정한다`

```env
OPENAI_API_KEY= ...

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=
POSTGRES_USER=postgres
POSTGRES_COLLECTION_NAME=mem0_vectors # 데이터베이스 이름
POSTGRES_PASSWORD=postgres

JWT_SECRET= ...
```

8. `(7) http://localhost:3000 에 접속하여 이메일과 비밀번호로 로그인한다.`
9. `(8) 왼쪽 대시보드 ACCOUNT/API keys 에서 api key를 발급받는다.`
10. `(9) cp finus_nat/.env.example finus/.env`
11. `(10) finus_nat/.env에서 Mem0 self-hosted 서버 설정을 위해 필요한 주석을 제거하고 MEM0_API_KEY를 채워넣는다.`
12. `(11) http://localhost:3000 대시보드의 ACCOUNT/Configuration 에서 LLM Provider를 Provider = openai, Model = gpt-5.4-mini 로 설정하고 API Key를 입력한다.`

## 2. Kis-trade-MCP

**Kis-Trade-MCP도 기본으로 Docker에서 구동할때 Port3000으로 설정되어있지만 Mem0와 충돌하므로 포트를 3300으로 변경한다.**

## 3. NAT chat CLI 테스트

bash finus_nat/scripts/run.sh 으로 실행하면 지속적으로 대화할 수 있습니다.
--memory 옵션을 사용하면 Mem0를 인식하며 finus_nat/configs/router.yml을 사용합니다.
--nomemory 옵션을 사용하면 finus_nat/configs/router_nomemory.yml을 사용합니다. 기본적으로 finus_nat/configs/router_nomemory.yml을 사용합니다.--once 옵션을 사용하면 한번 실행하고 워크플로우를 종료합니다.
