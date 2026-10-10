<div align="center">

# 🏆 Bolão da Champions 👻

---

## 📖 Sobre o projeto

Site desenvolvido em **Python com Flask** para gerenciar um bolão da **Champions League**, com sistema de palpites, classificação, atualização automática de resultados via API e fases de liga, playoffs e mata-matas.

<div align="left">

## ✨ Funcionalidades

- 💬 **Palpites direto do WhatsApp:** é só colar a mensagem do jogador (`Time 2 x 1 Time`) e o site monta a rodada e registra os palpites
- 🌐 **Resultados automáticos:** busca os placares reais da Champions na [football-data.org](https://www.football-data.org) e calcula a pontuação sozinho
- ⚔️ **Duelos entre os jogadores:** a liga e os playoffs são disputados em confrontos diretos (veja abaixo)
- 📋 **Classificação da liga:** pontos, vitórias, empates, derrotas e pontos de palpite
- 📈 **Acompanhamento visual:** barra de progresso da fase de liga e chaveamento das séries do playoff
- 📤 **Texto pronto para compartilhar:** gera a classificação para colar no grupo
- 👥 **Gestão de jogadores:** cadastro, remoção e ajuste de pontuação
- ✍️ **Correção manual:** dá para preencher ou corrigir um resultado à mão quando precisar

## 🎯 Como funciona

### Pontuação dos palpites

| Palpite | Pontos |
|---|:---:|
| Placar cravado (exato) | **5 pts** |
| Acertou só o resultado (vitória ou empate) | **3 pts** |
| Errou o resultado | **0 pts** |

### 🏟️ Fase de liga

- São **8 rodadas**, com os confrontos sorteados entre os jogadores, sem repetir adversários.
- Em cada rodada, quem somar **mais pontos de palpite** vence o duelo.
- Vitória vale **3 pontos**, empate **1** e derrota **0**.
- Critérios de desempate na tabela: **vitórias** e, depois, **pontos de palpite**.

### ⚔️ Playoffs

- Os **4 primeiros** colocados da liga vão direto para as quartas de final.
- Do **5º ao 12º colocado**, as outras vagas são disputadas em séries de **4 jogos**: 5º x 12º, 6º x 11º, 7º x 10º e 8º x 9º.
- Avança quem vencer mais duelos na série. Se terminar 2 x 2, passa quem ficou melhor colocado na liga.

### 🏅 Mata-matas

- As **8 vagas das quartas** são formadas pelos 4 classificados diretos e pelos 4 vencedores do playoff.

## 🛠️ Tecnologias

| Camada | Tecnologia |
|---|---|
| Back-end | ![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white) ![Flask](https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white) ![Gunicorn](https://img.shields.io/badge/Gunicorn-499848?style=for-the-badge&logo=gunicorn&logoColor=white) |
| Resultados das partidas | ![football-data.org](https://img.shields.io/badge/football--data.org-API-2E7D32?style=for-the-badge) |
| Hospedagem | ![Render](https://img.shields.io/badge/Render-46E3B7?style=for-the-badge&logo=render&logoColor=black) |
