export function Guide() {
  return <>
    <header><h1 tabIndex="-1">실습 가이드</h1><p>같은 주문으로 정상 동작과 장애 상황을 비교해 보세요.</p></header>
    <h2>첫 실습: Kafka 중단과 검색 지연</h2>
    <ol className="steps">
      <li><strong>정상 기준 만들기</strong> — <a href="#shop">스토어에서 주문</a>한 뒤 상세의 일반 조회를 두 번 눌러 MariaDB → Redis 변화를 확인합니다. 읽기 전용 대조로 같은 주문 ID와 버전이 검색에도 반영됐는지 확인합니다.</li>
      <li><strong>전달 중단하기</strong> — 터미널에서 <code>./all.sh mvp stop kafka --yes</code>를 실행한 뒤 새 주문을 만듭니다. 중단 전에 만든 주문과 구분하세요.</li>
      <li><strong>부분 동작 확인하기</strong> — 새 주문은 <a href="#orders">주문 원본</a>에 저장되지만 검색 반영은 지연됩니다. <a href="#health">연결 상태</a>의 전송 대기와 상세의 읽기 전용 대조를 확인하세요.</li>
      <li><strong>복구 확인하기</strong> — <code>./all.sh mvp resume kafka</code> 후 진단·대조를 새로고침합니다. 같은 주문 ID의 검색 버전이 원본과 일치하고 대기가 줄어드는지 확인한 뒤 <code>./all.sh mvp smoke</code>를 실행하세요.</li>
    </ol>
    <details><summary>데이터 흐름 이해하기</summary><div className="flow">쓰기: API → MariaDB [주문 + outbox] → worker → Kafka → worker → Elasticsearch<br/>상세 읽기: API → Redis HIT / MISS → MariaDB → Redis (TTL)<br/>상태 변경: MariaDB 버전 증가 → 검색 반영 · 캐시 무효화</div></details>
    <section className="card wide" id="scenarios"><details><summary>시나리오 자동 실행</summary><p className="hint">아래 명령은 터미널에서 실행합니다. seed는 요청 계획을 고정하며, 실제 지연·스레드 순서까지 같게 만들지는 않습니다. 결과는 reports/simulations에 기록됩니다.</p><pre>{`./all.sh mvp simulate list
./all.sh mvp simulate plan kafka-outage --seed 42
./all.sh mvp simulate run baseline --seed 42 --yes
./all.sh mvp simulate run kafka-outage --seed 42 --yes
./all.sh mvp simulate run row-lock --seed 42 --yes
./all.sh mvp simulate run version-race --seed 42 --yes
# 중단된 실습의 복원 마커가 남았을 때만:
./all.sh mvp simulate recover --yes`}</pre><p className="hint">plan은 DB를 변경하지 않습니다. run은 합성 주문을 남기고 지정한 장애를 실제로 주입합니다. 웹에서 장애 명령은 실행하지 않습니다.</p></details></section>
    <section className="card wide" id="terminal"><details><summary>터미널에서 장애·교체 실습</summary><p className="hint">한 번에 하나만 변경한 뒤 같은 주문으로 비교하세요. 웹 화면은 호스트 명령을 실행하지 않습니다.</p><pre>{`./all.sh mvp stop kafka --yes         # 주문은 저장, outbox 증가
./all.sh mvp resume kafka             # 전송 재개 후 검색 반영 확인
./all.sh mvp stop redis --yes         # 일반 조회가 MariaDB로 우회
./all.sh mvp resume redis
./all.sh mvp experiment redis-spare --yes       # 별도 Redis로 연결 대상 교체
./all.sh mvp experiment bad-db-password --yes   # API만 잘못된 DB 암호 사용
./all.sh mvp experiment fresh-search --yes      # 기존 인덱스를 보존하고 새 인덱스 사용
./all.sh mvp rebuild-search --yes               # MariaDB 원본으로 현재 검색 인덱스 재구축
./all.sh mvp experiment normal --yes           # 앱 연결 설정 복원
./all.sh mvp smoke                             # 실제 주문→검색 경로 확인`}</pre><p className="hint">normal은 앱 설정만 복원합니다. stop한 DB는 resume으로 별도 시작하세요. 컨테이너 재생성과 빈 데이터 저장소로 교체하는 것은 다릅니다.</p></details></section>
  </>;
}
