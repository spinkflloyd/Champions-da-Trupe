# Bolão Champions League — versão Python (Flask)

Versão do bolão baseada no formato definido para a Champions League: fase de liga com 8 rodadas, confrontos sorteados sem repetição, playoff do 5º ao 12º e classificação para as quartas.

## Regras implementadas

- **Fase de liga:** 8 rodadas. O site sorteia os confrontos entre os participantes e não repete o mesmo adversário nessas 8 rodadas.
- **Resultado de cada confronto:** quem fizer mais pontos de palpite na rodada vence. Se os pontos de palpite forem iguais, o confronto termina empatado.
- **Pontuação da tabela:** vitória = 3 pontos, empate = 1 ponto, derrota = 0 pontos.
- **Classificação:** 1º ao 4º vão diretamente para as quartas.
- **Playoff:** 5º×12º, 6º×11º, 7º×10º e 8º×9º. Cada série possui 4 jogos; quem vencer mais jogos avança. Em caso de 2×2, o melhor colocado na fase de liga fica com a vaga.
- **Pontuação antiga:** não é acumulada. Os pontos que existiam antes do novo formato foram removidos da classificação.
- **Futebol:** os resultados automáticos agora são buscados na competição **Champions League (CL)** da football-data.org.

## Como rodar

1. Instale Python 3.10+ e execute `pip install -r requirements.txt`.
2. Para resultados automáticos, configure `FOOTBALL_DATA_TOKEN`.
3. Execute `python app.py`.
4. Acesse `http://localhost:5000`.

O banco SQLite `bolao.db` é criado automaticamente. Use **Jogadores → Resetar torneio** para apagar dados antigos e iniciar o novo formato do zero.

## Fluxo

1. Cadastre/ajuste os participantes antes de começar.
2. Cole os palpites da primeira rodada da Champions League. Ela será vinculada automaticamente à Rodada 1 da fase de liga.
3. Repita até a Rodada 8.
4. Ao terminar as 8 rodadas, o chaveamento do playoff é criado automaticamente. As próximas 4 rodadas de futebol são usadas como os 4 jogos de cada série.
5. Os quatro vencedores do playoff completam as oito vagas das quartas junto com os quatro classificados diretos.

## Observação

A pontuação individual de cada jogo continua sendo: placar exato = 5 pontos; acerto apenas do resultado = 3 pontos; erro = 0. Esses pontos servem para decidir o vencedor do confronto entre participantes naquela rodada. Eles **não** são somados diretamente à tabela geral.
