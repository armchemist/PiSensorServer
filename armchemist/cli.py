"""배포 설정 검사, 데이터셋 시간 산출, GraphDB 인벤토리 가져오기."""
import argparse
import json
import os
from pathlib import Path

from .registry import duration_statistics
from .settings import Settings, download_models, validate_model
from .storage import LocalGraph, Neo4jGraph, Repository, validate_graph


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    parser.add_argument('--config', help='메인 YAML 경로 (기본 config/main.yaml)')
    check = sub.add_parser('check-config')
    check.add_argument('--models', action='store_true', help='로봇 PC의 설치된 체크포인트도 검사')
    download = sub.add_parser('download-models')
    download.add_argument('actions', nargs='*', help='생략하면 설정된 모든 ACT')
    export = sub.add_parser('export-ros')
    export.add_argument('output')
    serve = sub.add_parser('serve')
    serve.add_argument('--mode', choices=['mock', 'live'])
    duration = sub.add_parser('duration')
    duration.add_argument('info')
    duration.add_argument('episodes')
    duration.add_argument('--exclude', type=int, nargs='*', default=[])
    duration.add_argument('--dataset-revision', required=True)
    duration.add_argument('--output')
    inventory = sub.add_parser('import-inventory')
    inventory.add_argument('path')
    args = parser.parse_args()
    settings = Settings(args.config)
    if args.command == 'check-config':
        if args.models:
            for name in settings.actions:
                validate_model(settings, name)
        print(json.dumps({'config': str(settings.path), 'mode': settings.mode,
                          'configured_actions': list(settings.actions),
                          'transfer_limits_configured': bool(settings.robot['transfer_limits']),
                          'hardware_verified': False}, ensure_ascii=False, indent=2))
    elif args.command == 'download-models':
        names = args.actions or list(settings.actions)
        if set(names) - settings.actions.keys():
            parser.error('메인 YAML에 없는 ACT입니다')
        download_models(settings, names)
        print('모델 설치 및 관측 형식 검사 완료: ' + ', '.join(names))
    elif args.command == 'export-ros':
        import yaml
        Path(args.output).write_text(yaml.safe_dump(settings.ros_parameters(), sort_keys=False))
    elif args.command == 'serve':
        import uvicorn
        from .api import from_environment
        os.environ['AGENT_CONFIG'] = str(settings.path)
        if args.mode:
            os.environ['AGENT_MODE'] = args.mode
        uvicorn.run(from_environment(), **settings.raw['server'])
    elif args.command == 'duration':
        info = json.loads(Path(args.info).read_text())
        episodes = [json.loads(line) for line in Path(args.episodes).read_text().splitlines() if line.strip()]
        result = duration_statistics(episodes, info['fps'], args.exclude)
        result.update(dataset_revision=args.dataset_revision, duration_source=str(Path(args.episodes)))
        text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            Path(args.output).write_text(text)
        print(text, end='')
    else:
        value = json.loads(Path(args.path).read_text())
        validate_graph(value)
        graph_config = settings.raw['graph']
        if graph_config['uri']:
            graph = Neo4jGraph(graph_config['uri'], graph_config['user'],
                               settings.secret('graph', 'password_env', required=True), graph_config['database'])
            try:
                with graph.driver.session(database=graph.database) as session:
                    def replace(tx):
                        tx.run("MATCH (n:ArmchemistNode {snapshot:'inventory'}) DETACH DELETE n")
                        graph._save(tx, 'inventory', value)
                    session.execute_write(replace)
            finally:
                graph.driver.close()
        else:
            data = settings.resolve(settings.raw['data_dir'])
            data.mkdir(parents=True, exist_ok=True)
            repo = Repository(str(data / 'agent.sqlite3'), recover_runs=False)
            LocalGraph(repo)
            repo.put('graph', 'inventory', {**value, 'source': 'mock'})
        print('인벤토리를 저장했습니다.')


if __name__ == '__main__':
    main()
