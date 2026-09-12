import { BarChart3, Radar, Microscope, Sunrise, Activity, Flame } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { VibeReviewPage } from '@/pages/VibeReview'
import { VibeIntradayPage } from '@/pages/VibeIntraday'
import { VibeDeepDivePage } from '@/pages/VibeDeepDive'
import { VibeWatchPage } from '@/pages/VibeWatch'
import { VibeMarketPage } from '@/pages/VibeMarket'
import { VibeFirstBoardPage } from '@/pages/VibeFirstBoard'

const extension: FrontendExtension = {
  id: 'vibe.astock',
  apiVersion: 1,
  routes: [
    { id: 'vibe-review', path: '/vibe/review', component: VibeReviewPage },
    { id: 'vibe-market', path: '/vibe/market', component: VibeMarketPage },
    { id: 'vibe-firstboard', path: '/vibe/firstboard', component: VibeFirstBoardPage },
    { id: 'vibe-intraday', path: '/vibe/intraday', component: VibeIntradayPage },
    { id: 'vibe-deepdive', path: '/vibe/deepdive', component: VibeDeepDivePage },
    { id: 'vibe-watch', path: '/vibe/watch', component: VibeWatchPage },
  ],
  navigation: [
    { id: 'vibe-review', routeId: 'vibe-review', label: '短线复盘看板', icon: BarChart3, order: 200 },
    { id: 'vibe-market', routeId: 'vibe-market', label: '盘面数据', icon: Activity, order: 201 },
    { id: 'vibe-firstboard', routeId: 'vibe-firstboard', label: '首板分析', icon: Flame, order: 202 },
    { id: 'vibe-intraday', routeId: 'vibe-intraday', label: '开盘核验', icon: Sunrise, order: 203 },
    { id: 'vibe-deepdive', routeId: 'vibe-deepdive', label: '个股深挖', icon: Microscope, order: 204 },
    { id: 'vibe-watch', routeId: 'vibe-watch', label: '盯盘', icon: Radar, order: 205 },
  ],
}

export default extension
