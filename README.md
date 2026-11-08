# Jameson — o editor-chefe de bolso (alertas de notícias no Telegram)

Nome em homenagem a J. Jonah Jameson, editor do Clarim Diário (Homem-Aranha): quer a manchete já, sem enrolação.

Roda de hora em hora (cron). Pega só o que é **novo** em ~28 feeds RSS de veículos estabelecidos (BR + mundo:
mundo, Brasil, política, mercado, tecnologia, esportes), um LLM dá nota 1–10 e o código decide o que enviar.
Cada alerta = 1 mensagem: resumo de 1–2 frases + link da fonte. Sem envio entre 23h e 7h (o que sair à noite
é avaliado às 7h). Volume alvo: ~12–18 alertas/dia. Custo estimado com DeepSeek: ~US$ 1–2/mês.

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
    0 * * * * cd /home/SEU_USER/jameson && flock -n /tmp/jameson.lock ./venv/bin/python bot.py >> bot.log 2>&1

Cadastro de outras pessoas (opcional): defina `INVITE_CODE` no `.env`; quem mandar `/start <código>` ao bot passa a
receber os alertas, `/parar` sai. Por padrão o cadastro responde na próxima execução (até 1 h). Para responder em ~1 min (sem chamar o LLM), adicione ao cron:

    * * * * * cd /home/SEU_USER/jameson && flock -n /tmp/jameson.lock ./venv/bin/python bot.py --register >> bot.log 2>&1

Sem `INVITE_CODE`, só o dono (`TELEGRAM_CHAT_ID`) recebe. Assinantes ficam em `state.json`.

O horário de silêncio usa America/Sao_Paulo, independente do fuso da VPS.

## Subir na VPS
    git clone/scp para ~/jameson, instalar (acima), preencher o .env, testar com --dry-run --force
    ./venv/bin/python bot.py --force   # 1ª execução real; depois deixe o cron cuidar
    tail -f bot.log                    # acompanhar; cada execução loga tokens usados e itens enviados

## Trocar de modelo/provedor
Só `.env` (`LLM_MODEL=provedor:modelo` + a chave do provedor). Sem mudar código.

## Ajustar o gosto
No topo do `bot.py`: `INTERESTS`, `CATEGORY_RULES`, `SOURCES`, `DAILY_CAPS`, `QUIET_HOURS`.
`state.json` guarda o que já foi visto/enviado (apague para recomeçar).
