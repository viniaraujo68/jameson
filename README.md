# Jameson — o editor-chefe de bolso (alertas de notícias no Telegram)

Nome em homenagem a J. Jonah Jameson, editor do Clarim Diário (Homem-Aranha): quer a manchete já, sem enrolação.

Roda de hora em hora (cron). Pega só o que é **novo** em ~28 feeds RSS de veículos estabelecidos (BR + mundo:
mundo, Brasil, política, mercado, tecnologia, esportes), um LLM dá nota 1–10 e o código decide o que enviar.
Cada alerta = 1 mensagem: manchete em negrito + contexto + link da fonte. Dois passos: (1) o LLM escolhe e dá nota
com título+trecho do feed; (2) para quem vai ser enviado, o bot lê a matéria e reescreve manchete/contexto com o texto
completo (se a página não abrir, vale o rascunho do passo 1). Desdobramentos de uma história já enviada saem com
"🔁 atualização". Sob cada mensagem há botões 👍/👎 (ligue com `FEEDBACK_BUTTONS=1`). Sem envio entre 21h e 9h (o que sair à noite
é avaliado às 9h). Volume alvo: ~12–18 alertas/dia. Custo estimado com DeepSeek: ~US$ 1–2/mês.

## Instalar
    python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
    cp .env.example .env && chmod 600 .env        # preencha

Modelo recomendado (testado no dia simulado): `LLM_MODEL=deepseek:deepseek-flash` + `DEEPSEEK_API_KEY`, com
`LLM_THINKING=off` (~US$ 1/mês). Se a qualidade ficar abaixo do esperado, remova essa linha do `.env`: o raciocínio
ligado deu notícias melhores no teste (~US$ 2/mês).
Bot do Telegram: crie com o @BotFather (token) e pegue o chat id: mande uma mensagem ao seu bot e abra `https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id`.

## Testar
    ./venv/bin/python bot.py --dry-run --force    # chama o LLM, imprime, não envia nem grava estado
    ./venv/bin/python bot.py --force              # envio real (1º uso: só olha as últimas 3 h)

## Cron (de hora em hora; `flock` impede execuções sobrepostas)
    0 * * * * cd /home/SEU_USER/jameson && flock -n /tmp/jameson.lock ./venv/bin/python bot.py > /dev/null 2>> cron-error.log

Cadastro de outras pessoas (opcional): defina `INVITE_CODE` no `.env`; quem mandar `/start <código>` ao bot passa a
receber os alertas, `/parar` sai. Por padrão o cadastro responde na próxima execução (até 1 h). Para responder em ~1 min (sem chamar o LLM), adicione ao cron:

    * * * * * cd /home/SEU_USER/jameson && flock -n /tmp/jameson.lock ./venv/bin/python bot.py --register > /dev/null 2>> cron-error.log

Sem `INVITE_CODE`, só o dono (`TELEGRAM_CHAT_ID`) recebe. Assinantes ficam em `state.json`.

O horário de silêncio usa America/Sao_Paulo, independente do fuso da VPS.

## Subir na VPS
    git clone/scp para ~/jameson, instalar (acima), preencher o .env, testar com --dry-run --force
    ./venv/bin/python bot.py --force   # 1ª execução real; depois deixe o cron cuidar
    tail -f bot.log                    # acompanhar (rotação automática: 1 MB x 3); cada execução loga tokens usados e itens enviados

## Trocar de modelo/provedor
Só `.env` (`LLM_MODEL=provedor:modelo` + a chave do provedor). Sem mudar código.

## Ajustar o gosto
No topo do `bot.py`: `INTERESTS`, `CATEGORY_RULES`, `SOURCES`, `DAILY_CAPS`, `QUIET_HOURS`.
`state.json` guarda o que já foi visto/enviado (apague para recomeçar).

Arquivos gerados (fora do Git): `picks.jsonl` (toda escolha do modelo, com nota e se foi enviada), `feedback.jsonl`
(seus 👍/👎), `bot.log` (rotativo), `cron-error.log`. Servem para calibrar o prompt com dados reais.
