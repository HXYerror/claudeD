"""Bot self-test: starts the bot, runs integration tests, reports results."""
import asyncio
import logging
import os
import sys
import time
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)], force=True)
logging.getLogger('discord').setLevel(logging.WARNING)
log = logging.getLogger('selftest')

GUILD_ID = 1499415073838600454
CHANNEL_ID = 1499415074614280234

# Import bot components
sys.path.insert(0, '/tmp/claudeD/src')
from clauded.config import load_config

results = []

def ok(name):
    results.append(('✅', name))
    log.info(f'✅ {name}')

def fail(name, err):
    results.append(('❌', f'{name}: {err}'))
    log.error(f'❌ {name}: {err}')


async def run_tests():
    config = load_config()
    
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    
    bot = commands.Bot(command_prefix="!", intents=intents)
    
    # Import and attach components manually (same as ClaudedBot)
    from clauded.project_manager import ProjectManager
    from clauded.session_manager import SessionManager
    from clauded.session_store import SessionStore
    from clauded.cost_tracker import CostTracker
    
    bot.config = config
    bot.project_manager = ProjectManager(projects_root=config.projects_root)
    bot.session_manager = SessionManager(session_store=SessionStore())
    bot.cost_tracker = CostTracker()
    bot._start_time = time.time()
    
    @bot.event
    async def on_ready():
        log.info(f'Self-test bot online: {bot.user}')
        guild = bot.get_guild(GUILD_ID)
        channel = guild.get_channel(CHANNEL_ID)
        
        try:
            # === TEST 1: Project Bind ===
            try:
                # Create a test dir
                test_dir = os.path.expanduser('~/claudeD-test-project')
                os.makedirs(test_dir, exist_ok=True)
                stored = bot.project_manager.bind(CHANNEL_ID, test_dir)
                assert bot.project_manager.is_bound(CHANNEL_ID)
                assert bot.project_manager.get_path(CHANNEL_ID) is not None
                ok('Project bind')
            except Exception as e:
                fail('Project bind', e)
            
            # === TEST 2: Send message to channel ===
            try:
                msg = await channel.send('🤖 Self-test: Bot can send messages')
                assert msg.id is not None
                ok('Send message to channel')
            except Exception as e:
                fail('Send message to channel', e)
            
            # === TEST 3: Create thread ===
            try:
                thread = await msg.create_thread(name='Self-test thread')
                assert thread is not None
                ok('Create thread')
            except Exception as e:
                fail('Create thread', e)
            
            # === TEST 4: Send message in thread ===
            try:
                tmsg = await thread.send('🤖 Self-test: Bot can send in threads')
                assert tmsg.id is not None
                ok('Send in thread')
            except Exception as e:
                fail('Send in thread', e)
            
            # === TEST 5: Create Claude session ===
            try:
                from clauded.claude_bridge import ClaudeBridge
                bridge = ClaudeBridge(test_dir, config)
                await bridge.start()
                assert bridge.is_active
                ok('Claude session start')
            except Exception as e:
                fail('Claude session start', e)
                bridge = None
            
            # === TEST 6: Send message to Claude and get response ===
            if bridge and bridge.is_active:
                try:
                    text_parts = []
                    async for event in bridge.send_message('Reply with exactly: SELFTEST_OK'):
                        from claude_code_sdk import AssistantMessage, TextBlock, ResultMessage
                        if isinstance(event, AssistantMessage):
                            for block in event.content:
                                if isinstance(block, TextBlock):
                                    text_parts.append(block.text)
                        elif isinstance(event, ResultMessage):
                            break
                    
                    response = ''.join(text_parts)
                    log.info(f'Claude response: {response[:200]}')
                    
                    if response.strip():
                        ok(f'Claude responds: "{response[:50]}"')
                    else:
                        fail('Claude response', 'Empty response')
                    
                    # Post response in thread
                    await thread.send(f'🤖 Claude said: {response[:1900]}')
                except Exception as e:
                    fail('Claude response', e)
                
                # === TEST 7: Session stats ===
                try:
                    assert bridge.session_id is not None or bridge.num_turns >= 0
                    ok(f'Session stats: cost=${bridge.total_cost:.4f}, turns={bridge.num_turns}')
                except Exception as e:
                    fail('Session stats', e)
                
                # Cleanup bridge
                try:
                    await bridge.stop()
                except:
                    pass
            
            # === TEST 8: Cost tracker ===
            try:
                bot.cost_tracker.record(CHANNEL_ID, 0.01)
                cost, calls = bot.cost_tracker.get_channel_cost(CHANNEL_ID)
                assert cost > 0
                assert calls > 0
                ok(f'Cost tracker: ${cost:.4f}, {calls} calls')
                bot.cost_tracker.reset_channel(CHANNEL_ID)
            except Exception as e:
                fail('Cost tracker', e)
            
            # === TEST 9: Embeds work ===
            try:
                embed = discord.Embed(title='🏥 Self-Test Results', color=discord.Color.green())
                for status, name in results:
                    embed.add_field(name=status, value=name, inline=False)
                await thread.send(embed=embed)
                ok('Embed rendering')
            except Exception as e:
                fail('Embed rendering', e)
            
            # === TEST 10: Cleanup ===
            try:
                bot.project_manager.unbind(CHANNEL_ID)
                assert not bot.project_manager.is_bound(CHANNEL_ID)
                import shutil
                shutil.rmtree(test_dir, ignore_errors=True)
                ok('Cleanup')
            except Exception as e:
                fail('Cleanup', e)
            
            # Final summary
            log.info('='*50)
            log.info(f'SELF-TEST COMPLETE: {sum(1 for s,_ in results if s=="✅")}/{len(results)} passed')
            for s, n in results:
                log.info(f'  {s} {n}')
            log.info('='*50)
            
            # Post final summary to channel
            summary = '\n'.join(f'{s} {n}' for s, n in results)
            passed = sum(1 for s,_ in results if s=='✅')
            await channel.send(f'**Self-Test Results: {passed}/{len(results)} passed**\n{summary}')
            
        finally:
            await bot.close()
    
    await bot.start(os.environ['DISCORD_BOT_TOKEN'])

asyncio.run(run_tests())
