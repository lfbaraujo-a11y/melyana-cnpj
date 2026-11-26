#!/bin/bash
# Roda a carga da base de CNPJs no Cloud Shell do Google
# e grava direto no BigQuery. Uso:  bash rodar.sh
cd "$(dirname "$0")"
codigo=$(curl -s -m 30 -o /dev/null -w "%{http_code}" -u gn672Ad4CF8N6TK: -X PROPFIND -H "Depth: 0" \
  https://arquivos.receitafederal.gov.br/public.php/webdav/)
if [ "$codigo" != "207" ]; then
  echo "A Receita não respondeu daqui (código $codigo). Avise o Claude."
  exit 1
fi
echo "Receita respondeu. Instalando o que falta..."
pip3 install --user -q google-cloud-bigquery
VARS="DESTINO=bigquery RECORTE=${RECORTE:-brasil-ativos} BQ_PROJECT=${BQ_PROJECT:-genuine-ether-464218-q6}"
tmux kill-session -t carga 2>/dev/null
tmux new -d -s carga "cd $PWD && env $VARS python3 carregar_cnpj.py 2>&1 | tee $HOME/carga-cnpj.log"
echo "Carga iniciada (leva cerca de 1 a 2 horas). Deixe esta aba aberta."
sleep 3
tail -f $HOME/carga-cnpj.log
