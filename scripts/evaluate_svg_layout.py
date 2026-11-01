"""Exercise SVG generation across a local PDF corpus and retain QA artifacts.

The corpus JSON contains objects with `id` and `path`. API credentials are read
from DEEPSEEK_API_KEY or an interactive password prompt, never written to disk.
This isolates manuscript-to-SVG behavior using source excerpts; it is not a
benchmark of the production PDF figure extractor or a complete-deck quality score.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import subprocess
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz
from openai import AsyncOpenAI
from PIL import Image

from backend.generator.svg_critic import check_svg
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_to_pptx.builder import create_pptx
from backend.generator.visual_critic import render_svg_to_png
from backend.llm import LLMMessage, LLMResponse, TokenUsage
from backend.orchestrator.strategist_agent import create_design_spec
from backend.orchestrator.svg_executor import generate_svg_pages_parallel


def source_excerpts(path: Path) -> tuple[str, dict]:
    """Sample opening, methods and results pages with their source page numbers."""
    with fitz.open(path) as document:
        texts = [page.get_text('text', sort=True) for page in document]
        selected = set(range(min(2, len(texts))))
        for pattern in (r'method|approach|architecture|方法|模型', r'results|experiment|evaluation|结果|实验|精度'):
            ranked = sorted(range(1, len(texts)), key=lambda index: len(re.findall(pattern, texts[index], re.I)), reverse=True)
            selected.update(ranked[:2])
        source = '\n\n'.join(f'## PDF page {index+1}\n{texts[index][:7500]}' for index in sorted(selected))
        metadata = {'filename': path.name, 'pages': len(document), 'sampled_pages': [index+1 for index in sorted(selected)],
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        return source, metadata


def json_response(text: str) -> dict:
    text = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', text.strip())
    return json.loads(text)


class EvaluationProvider:
    def __init__(self, client: AsyncOpenAI, semaphore: asyncio.Semaphore, directory: Path):
        self.client, self.semaphore, self.directory = client, semaphore, directory
        self.stage = 'manuscript'
        self.calls: list[dict] = []

    async def chat(self, messages, model, **kwargs):
        index = len(self.calls) + 1
        entry = {'call': index, 'stage': self.stage}
        self.calls.append(entry)
        payload = [{'role': message.role, 'content': message.content} for message in messages]
        (self.directory / f'prompt_{index:02d}.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf8')
        async with self.semaphore:
            started = time.perf_counter()
            response = await self.client.chat.completions.create(
                model=model, messages=payload, max_tokens=min(kwargs.get('max_tokens', 16000), 16000),
                temperature=kwargs.get('temperature', .5), extra_body={'thinking': {'type': 'disabled'}},
            )
            entry['seconds'] = round(time.perf_counter() - started, 2)
        choice, usage = response.choices[0], response.usage
        entry.update(model=response.model, finish_reason=choice.finish_reason,
                     prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                     cached_tokens=getattr(usage, 'prompt_cache_hit_tokens', None))
        content = choice.message.content or ''
        (self.directory / f'response_{index:02d}.md').write_text(content, encoding='utf8')
        (self.directory / 'calls.json').write_text(json.dumps(self.calls, indent=2), encoding='utf8')
        print(f'{self.directory.name}: {self.stage} call {index}, {entry["seconds"]}s', flush=True)
        return LLMResponse(content=content, usage=TokenUsage(usage.prompt_tokens, usage.completion_tokens),
                           raw={'choices': [{'finish_reason': choice.finish_reason}]})


def render_and_audit(directory: Path) -> list[dict]:
    reports, images = [], []
    for path in sorted((directory / 'svg_output').glob('*.svg')):
        prepared = prepare_svg_file_content_for_render(path)
        png = render_svg_to_png(prepared)
        if png:
            image_path = directory / (path.stem + '.png')
            image_path.write_bytes(png)
            with Image.open(image_path) as image:
                images.append(image.convert('RGB').copy())
        report = check_svg(prepared).to_dict()
        reports.append({'page': path.stem, **report})
    if images:
        sheet = Image.new('RGB', (max(image.width for image in images), sum(image.height for image in images)), 'white')
        offset = 0
        for image in images:
            sheet.paste(image, (0, offset))
            offset += image.height
        sheet.save(directory / 'contact_sheet.png')
    create_pptx(sorted((directory / 'svg_output').glob('*.svg')), directory / 'samples.pptx')
    (directory / 'geometry.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf8')
    return reports


async def evaluate_one(item: dict, args, client, semaphore) -> dict:
    directory = args.output / item['id']
    directory.mkdir(parents=True, exist_ok=True)
    summary = {'id': item['id'], 'file': Path(item['path']).name}
    try:
        source, metadata = await asyncio.to_thread(source_excerpts, Path(item['path']))
        (directory / 'source.md').write_text(source, encoding='utf8')
        (directory / 'source.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf8')
        provider = EvaluationProvider(client, semaphore, directory)
        if args.reuse_fixtures:
            fixture = json.loads((args.reuse_fixtures / item['id'] / 'fixture.json').read_text(encoding='utf8'))
        else:
            response = await provider.chat([
            LLMMessage.system('Create a source-grounded three-page research slide manuscript in Simplified Chinese. '
                'Return JSON with a pages array, each item containing role, title, body (Markdown), and source_pages. '
                'Page roles are cover, content, content. Cover content consists of the source title and supplied author/venue metadata. '
                'The first content page explains the method; the second explains evidence or results. '
                'Use the source title and author metadata. Include an equation or a compact numeric table when the excerpts provide one. '
                'Keep values, units, comparison conditions, and mathematical notation faithful to the cited source pages. '
                'Make each page coherent and concise, with enough information to test the rendering of real research material.'),
            LLMMessage.user(source),
            ], args.model, max_tokens=5000)
            fixture = json_response(response.content)
        if len(fixture['pages']) != 3:
            raise ValueError('Expected three manuscript pages')
        manuscript = '\n\n---\n\n'.join(
            f'<!-- page_type: {page["role"]} -->\n# {page["title"]}\n\n{page["body"]}' for page in fixture['pages'])
        (directory / 'fixture.json').write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding='utf8')
        (directory / 'manuscript.md').write_text(manuscript, encoding='utf8')
        provider.stage = 'design'
        design = await create_design_spec(manuscript, provider, args.model, language='zh', debug_dir=directory / 'debug')
        (directory / 'design_spec.md').write_text(design, encoding='utf8')
        provider.stage = 'svg'
        critic_events = []
        async def on_critic(*event):
            critic_events.append({'page': event[0], 'attempt': event[1], 'report': event[2].to_dict()})
        async for _page, _svg in generate_svg_pages_parallel(design, manuscript, directory, provider, args.model,
            language='zh', mode='page_parallel', parallel_concurrency=3, max_critic_attempts=0,
            enable_visual_critic=False, on_critic=on_critic):
            pass
        (directory / 'critic_events.json').write_text(json.dumps(critic_events, ensure_ascii=False, indent=2), encoding='utf8')
        reports = await asyncio.to_thread(render_and_audit, directory)
        provider.stage = 'fidelity'
        svg_sources = '\n\n'.join(f'## {path.name}\n{path.read_text(encoding="utf8")}' for path in sorted((directory / 'svg_output').glob('*.svg')))
        review = await provider.chat([
            LLMMessage.system('Review factual fidelity of research slides against the supplied source excerpts and manuscript. '
                'Return JSON with unsupported_claims, unsupported_data_marks, and missing_material arrays. '
                'Each finding includes slide, observation, and source_basis. '
                'Check values, equations, comparative conditions, and whether visual data marks have supplied values. '
                'Distinguish abstract unlabeled decoration, structural schematics, scale guides, and data-bearing marks. '
                'Report demonstrated discrepancies. Return empty arrays for categories with no demonstrated discrepancy.'),
            LLMMessage.user(f'## Source excerpts\n{source}\n\n## Manuscript\n{manuscript}\n\n## SVGs\n{svg_sources}'),
        ], args.model, max_tokens=4000, temperature=.1)
        (directory / 'fidelity.json').write_text(json.dumps(json_response(review.content), ensure_ascii=False, indent=2), encoding='utf8')
        audit = json.loads((directory / 'samples.export_audit.json').read_text(encoding='utf8'))
        summary.update(status='generated', slides=len(reports), export=audit['summary'],
            geometry_errors=sum(report['error_count'] for report in reports), calls=len(provider.calls))
    except Exception as error:
        summary.update(status='failed', error=f'{type(error).__name__}: {error}')
    (directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return summary


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(args.corpus.read_text(encoding='utf8'))
    if args.limit:
        corpus = corpus[:args.limit]
    if args.recheck:
        results = []
        for item in corpus:
            directory = args.output / item['id']
            try:
                reports = render_and_audit(directory)
                results.append({'id': item['id'], 'geometry_errors': sum(report['error_count'] for report in reports), 'status': 'passed'})
            except Exception as error:
                results.append({'id': item['id'], 'status': 'failed', 'error': f'{type(error).__name__}: {error}'})
        (args.output/'recheck.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf8')
        return
    repository = Path(__file__).resolve().parents[1]
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True, capture_output=True, check=True).stdout.strip()
    source_paths = [*repository.glob('backend/**/*.py'), *repository.glob('backend/orchestrator/prompts/*.md'),
                    repository/'assets/references/shared-standards-essential.md',
                    repository/'assets/templates/design_spec_reference.md', Path(__file__).resolve()]
    run = {'started_at': datetime.now(timezone.utc).isoformat(), 'git_revision': revision,
           'model': args.model, 'thinking': 'disabled', 'concurrency': args.concurrency,
           'reuse_fixtures': str(args.reuse_fixtures) if args.reuse_fixtures else None,
           'source_hashes': {str(path.relative_to(repository)).replace('\\','/'): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}}
    (args.output/'run.json').write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf8')
    key = os.environ.get('DEEPSEEK_API_KEY') or getpass.getpass('API key (not saved): ')
    client = AsyncOpenAI(api_key=key, base_url=args.base_url, max_retries=0, timeout=240)
    try:
        semaphore = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*(evaluate_one(item, args, client, semaphore) for item in corpus))
        (args.output / 'summary.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf8')
    finally:
        await client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='deepseek-v4-flash')
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--reuse-fixtures', type=Path, help='Reuse prior manuscripts for a controlled SVG comparison')
    parser.add_argument('--recheck', action='store_true', help='Rerender and audit existing SVGs without model calls')
    asyncio.run(main(parser.parse_args()))
